## A. Findings both earlier reviews missed

- **P1 — Retryable zero-cost AudD outcomes are incorrectly treated as permanently finished.** `recover_paid_attempts` adds every resolved query to `resolved_query_ids`, regardless of outcome (`src/id_detector/service.py:369-375`). The sweep then skips any such query without a cache body (`src/id_detector/paid_clip.py:544-564`). Failure scenario: an attempt resolves `http_429`, `http_503`, `connect_error`, or `timeout_pre`, then the worker crashes before its prescribed retry. Resume never retries that window, contrary to PLAN §2.3.3, and can end `partial`.

- **P1 — Recovered spend does not prevent an illegal `--allow-degrade` substitution.** The decision checks only `primary_clip.billable_units` from the current pass (`src/id_detector/pipeline.py:916-973`), not `restored_units` or cumulative admitter spend. Failure scenario: pass one records a billable `http_5xx`, then crashes; on resume that query is skipped, the next request gets `auth_error`, and the run restarts as Free despite already-paid work. If the Shazam breaker is open, it instead becomes `waiting` with paid spend, rather than retaining the Deep failure.

- **P1 — An interrupted, but not checkpointed, Shazam secondary is neither restored nor protected from same-run re-query.** Only a completed `secondary` checkpoint restores evidence (`src/id_detector/pipeline.py:1148-1202`). If the breaker is now open, the non-checkpointed observations are discarded (`src/id_detector/pipeline.py:1211-1217`); if closed, cached `no_match` jobs are reset under the default refresh state (`src/id_detector/recognise.py:454-460`). Failure scenario: the first secondary probe finishes and the process dies during confirmations; resume either loses that evidence or resends its known no-matches.

- **P1 — Local checkpoints prove existence, not durability, and are trusted without revalidation.** `LocalCheckpointStore.write` checks only `path_is_file` before committing (`src/id_detector/service.py:252-270`), while decode publishes PCM with an unsynced `os.replace` (`src/id_detector/decode.py:129-165`) and POSIX atomic writes do not fsync the containing directory (`src/id_detector/io.py:80-125`). `completed_phases` later trusts the phase key without checking the recorded artefacts (`src/id_detector/service.py:238-275`). A power loss can therefore leave a durable checkpoint naming a missing PCM/window artefact, after which resume skips the phase instead of redoing it.

## B. Earlier findings whose fix is incomplete or wrong

- **P0 — Report P0-3 still re-dispatches ambiguous attempts.** Recovery correctly identifies them (`src/id_detector/service.py:376-383`), but the sweep consults only `resolved_query_ids`; an unresolved dispatched query reaches `ledger.dangling` and is dispatched again (`src/id_detector/paid_clip.py:548-585`). Worse, same-run retry ordinal restarts at zero, so the deterministic attempt ID can collide with the original (`src/id_detector/attempts.py:39-42`, `src/id_detector/paid_clip.py:570-583`). Failure scenario: kill the process after `dispatched` but before `resolved`; resume pre-charges the ambiguous unit and then sends that clip to AudD again. A subsequent fold may represent both network calls as one resolved attempt. This directly contradicts the report’s claimed P0-3 fix (`docs/reviews/build-4a-i.md:241-251`).

- **P0 — Report P0-3/P1-9 does not actually recover the original reservation or original price.** An interrupted primary has no checkpointed reservation; resume calls `reserve_usd` again from current configuration (`src/id_detector/pipeline.py:703-742`). Recovery reduces monetary history to a unit count even though each attempt records `unit_usd_e6` (`src/id_detector/service.py:364-383`; `src/id_detector/attempts.py:95-107`). The lowered-cap path multiplies those units by today’s price (`src/id_detector/pipeline.py:713-718`). Failure scenario: four requests were billed at 5,000 µUSD, then pricing changes to 4,000 before resume; cumulative spend becomes 16,000 instead of 20,000 µUSD. An injected fresh `usd_admitter` is also never pre-charged because restoration is conditional on `usd_admitter is None` (`src/id_detector/pipeline.py:737-742`).

- **P0 — The “compatible result spends zero” fix loses money for a resumed run that already spent.** Compatibility serving occurs before attempt recovery (`src/id_detector/pipeline.py:603-625` versus `:686-694`) and unconditionally reports zero. Failure scenario: AudD work completes, `publish_result` seals a compatible bundle, and the process dies before the `present` checkpoint or invocation append (`src/id_detector/pipeline.py:1431-1454`, `:1479-1503`). Resuming the same `run_id` serves that bundle with zero spend and no settlement journal. The report’s cache-hit fix (`docs/reviews/build-4a-i.md:224-226`) covered only a genuinely fresh cache-hit run.

- **P1 — Report P1-5 permits more than one settlement for the same resumed run.** Cancellation settles and journals the run (`src/id_detector/pipeline.py:1534-1549`); because no primary checkpoint exists, resume constructs a fresh reservation and later settles the same `run_id` again. The tests explicitly allow multiple invocation entries for one run (`tests/test_service_api.py:101-102`) and exercise cancel-then-resume without asserting a single settlement (`tests/test_service_api.py:554-593`). This is not “settle exactly once.”

- **P1 — Report P0-1’s claim that every terminal path yields a `RunResult` is false for `failed`.** The pipeline records failure and re-raises (`src/id_detector/pipeline.py:1550-1565`); `service.run` suppresses only `CancelledError`, so it never reaches `RunResult` construction (`src/id_detector/service.py:513-575`). The web runner consequently has no outcome and records spend as unknown (`src/id_detector/webapp/runner.py:103-106`, `:366-372`). Failure scenario: a checkpoint or publication exception after paid primary; CLI bypasses the shared exit mapping and the web job loses the service-reported status/spend. This contradicts `docs/reviews/build-4a-i.md:213-220`.

- **P1 — Report P0-2’s “no control characters” fix rejects only CR, LF, and NUL.** `validate_platform_url` omits the rest of C0 and DEL (`src/id_detector/service.py:118-141`). Inputs such as `https://example.com/a\u0001b` or an embedded tab are accepted and handed onward. I did not find a direct filesystem path that passes the scheme/host checks, but the stated hosted-boundary validation remains incomplete (`docs/reviews/build-4a-i.md:231-239`).

- **P2 — The extraction still violates the post-Phase-3 change boundary and leaves a direct pipeline entry point.** The commit added `pipeline.py` and modified `cli.py`, `paid_clip.py`, and `webapp/runner.py`, although PLAN §4.2 limits changes under `src/id_detector/` to `service.py`, `recipes.py`, the serve entry point, and `PROJECT_ROOT`. `cli.py` publicly re-exports the pipeline (`src/id_detector/cli.py:48-58`), and `idea rescan` invokes it directly (`src/id_detector/cli.py:942-963`). Thus `service.run` is not, as the report claims, the only analysis path (`docs/reviews/build-4a-i.md:290-304`).

- **P2 — `RunResult.attempts` does not consistently mean “dispatches this pass,” contrary to the report.** A completed-primary resume restores the earlier attempt count into `counts["paid_attempts"]` (`src/id_detector/pipeline.py:823-848`), which `_finish` returns (`:472`), despite making zero calls. An interrupted-primary resume reports only new-pass attempts. This contradicts `docs/reviews/build-4a-i.md:306-308`.

## C. Test gaps

- No same-`run_id` hard-kill test stops after `dispatched` and before `resolved`; the older test instead codifies automatic ambiguous re-dispatch (`tests/test_phase0b_attempts.py:308-348`).
- The 4a-i crash helper fires only after a window resolves (`tests/test_service_api.py:131-138`), so it cannot expose an ambiguous attempt.
- No resume test changes pricing or verifies the original reservation fields.
- No test supplies an injected admitter on resume or checks journal/admitter `run_id` and unit-price consistency.
- No test resumes after a compatible bundle became durable but before terminal accounting.
- No failure-after-paid-work test requires `service.run` to return `RunResult(status="failed")`.
- The breaker test crashes only after `secondary` and `fuse2` are checkpointed (`tests/test_service_api.py:665-706`), not during secondary.
- URL tests omit embedded C0 controls, tab, and DEL (`tests/test_service_api.py:318-348`).
- Checkpoint tests assert only file existence (`tests/test_service_api.py:230-247`), not fsync, hashes, read-time validation, or power-loss ordering.
- No assertion defines consistent resumed-run semantics for `RunResult.attempts`.

## D. What you verified is correct

- The public request/result field lists match PLAN §4.3 exactly (`src/id_detector/service.py:286-311`).
- The nine checkpoint names and normal write order are exactly `ingest, decode, windows, primary, hints, fuse1, secondary, fuse2, present`.
- `idea analyse` and the legacy web runner both call `service.run` and use `exit_code_for` (`src/id_detector/cli.py:683-750`; `src/id_detector/webapp/runner.py:248-327`). The runner’s journal read is used only for stage timing, not status or spend.
- Hosted `LocalPath` is refused; the listed filesystem, UNC, `file:`, drive-letter, credential, missing-host, and traversal-shaped `PlatformUrl` cases are rejected. `UploadId` uses a strict alphabet and the local resolver performs resolved-path containment.
- Resolved same-run AudD `match` and cached `no_match` outcomes are reused without dispatch when their raw cache body exists.
- Phase callbacks occur after their phase writers return; the defect is physical durability/read-time validation, not the basic source-order placement.
- At current committed `HEAD`, `service.py`, `pipeline.py`, `paid_clip.py`, `webapp/runner.py`, and the relevant service/attempt tests are unchanged from `02d08b9`; the only same-file later change is an unrelated truth-review command in `cli.py`. These findings therefore persist.
- This was a static, read-only review. I did not run `uv`, pytest, a server, GC, the pipeline, or any provider call. The 4a-ii working-tree changes and separately owned files were excluded.

## Required follow-ups

1. **[P0]** Make dispatched-without-resolved attempts terminal ambiguous/spent on automatic resume; dispatch only prepared-without-dispatched attempts, using a fresh non-colliding ordinal.
2. **[P0]** Persist the original reservation and exact per-attempt µUSD before dispatch, then restore that reservation rather than recomputing it from current pricing or caps.
3. **[P0]** Recover and settle same-run money before any compatible-result early return; add the crash-after-bundle-publication case.
4. **[P1]** Make cancellation/resume use one monotonic reservation and one terminal settlement event per `run_id`.
5. **[P1]** Distinguish retryable zero-cost resolutions from terminal/billable resolutions during recovery, and base `allow_degrade` on cumulative spend.
6. **[P1]** Ensure every real terminal failure produces the contracted `RunResult`, including status and settled spend.
7. **[P1]** Restore partial secondary evidence and suppress same-run no-match refresh before consulting an open Shazam breaker.
8. **[P1]** Reject every C0 control and DEL in `PlatformUrl`, with explicit parser-differential tests.
9. **[P1]** Fsync phase artefacts and directories before checkpoint commit, record hashes, and revalidate them before treating a phase as complete.
10. **[P2]** Remove or formally authorize direct pipeline entry points, and define one consistent meaning for `RunResult.attempts`.

VERDICT: FOLLOW_UP_REQUIRED