# Follow-up — money and resume safety (retro-reviews 4a-i and 4b-i)

Base: `ec32f97` (includes 4a-ii `3cb5dc2`). Worktree branch `worktree-agent-ac7462893536c29b5`,
changes uncommitted. Everything ran offline (`IDEA_TEST_MODE=1`, fake AudD/Shazam); no real URL,
no `.env`, no GC, no provider call.

The worktree was created at `9bfacf8`, a strict ancestor of `ec32f97` with no commits of its own
and a clean tree; it was fast-forwarded to `ec32f97` (`git reset --hard ec32f97`) before any work.

## Method

1. **Reproduce first.** Every P0/P1 of both retro-reviews got a behavioural test written against
   `ec32f97` before any production change: `tests/test_followup_money_resume.py` (service seam,
   4a-i) and `tests/idea_web/test_followup_queue_money.py` (queue, 4b-i). At `ec32f97`: **14/14**
   service reproductions and **15/15** queue reproductions failed, each on the defect (outputs
   below). One queue test was later split in two because its second half could not run at HEAD
   (see D1); the split half was proven load-bearing by reverting only its one clause.
2. A crash is modelled by `_HardKill(BaseException)`: neither the pipeline's `except Exception` nor
   its cancellation handler runs, exactly as for a killed process with a `dispatched` line on disk.
3. **Fix with one shared implementation** — `src/id_detector/run_ledger.py` — used by both layers.

## The shared implementation: `id_detector.run_ledger`

One module owns the four money/resume facts; the service seam, the pipeline, the paid sweep and the
queue worker all call it, so the two layers cannot disagree about whether a request was paid for.

| Fact | API | Used by |
|---|---|---|
| Attempt fold over any durable source (JSONL, SQLite rows, both), merged by `(attempt_id, event)`; conflicting duplicate or self-parent → `LedgerConflict` | `fold_run_ledger`, `events_from_jsonl`, `event_from_row`, `parse_journal_lines` | `AttemptJournal.run_ledger` / `SQLiteAttemptJournal.durable_events`, `service.recover_paid_attempts`, `pipeline`, worker `fold_run_money` |
| Per-query resume rule (`fresh`/`reuse`/`settled`/`retry`/`ambiguous`/`reissue`) with `next_ordinal = 1 + max(ordinal)` | `RunLedger.resume` | `paid_clip._process` |
| Money: `Σ unit_usd_e6` of billable resolutions + ambiguous dispatches, each at its own recorded price | `RunLedger.spent_usd_e6`, `RecoveredMoney`, `recovered_money`, `restore_admitter` (+ `UsdAdmitter.restore_spent`) | pipeline settlement, worker cancel/dead-letter/terminal |
| Reservation: written once, durably, before the first dispatch; restored verbatim | `ReservationRecord`, `write_reservation`, `read_reservation` (+ `AttemptJournal.record_reservation`/`durable_reservation`; SQLite override also stores `checkpoints._reservation`) | pipeline Deep branch, worker fold |

## Findings — verdicts, evidence, tests, fixes

"HEAD evidence" is the assertion that failed at `ec32f97`.

### Retro-review 4a-i

| Tag | Finding | Verdict | HEAD evidence | Regression test | Fix |
|---|---|---|---|---|---|
| B P0 | Ambiguous same-run attempt re-dispatched; ordinal restarts at 0 (id collision, self-parent) | **Reproduced** | `an attempt id was reused inside one run`; probe: 4 dispatched/1 unresolved, resume re-sent the ambiguous window 2, ran out of reservation → `partial`; 8 prepared lines, 7 unique ids, 3 self-parented events | `test_a_same_run_ambiguous_attempt_is_spent_and_never_sent_again` | `RunLedger.resume` → `ambiguous` is spent and never re-sent; re-sent queries get `1+max(ordinal)`; `AttemptJournal.prepare` refuses self-parent and a reused id |
| B P0 | Reservation/price recomputed on resume; injected admitter not pre-charged | **Reproduced** | `assert 29400 == 36750`; lowered cap → `budget_exhausted` with `usd_e6_reserved=0`; injected admitter `assert 15000 == 35000` | `test_resume_restores_the_original_reservation_and_price_not_todays`, `test_a_lowered_cap_does_not_recompute_a_durable_reservation`, `test_an_injected_admitter_on_resume_is_charged_with_the_recovered_spend` | Reservation recorded durably before the sweep; restored verbatim on resume; admitter (created or injected, unit-checked) charged with exact ledger µUSD |
| B P0 | Compatible-result early return reports zero for a run that already spent | **Reproduced** | `assert 0 == (7 * 5000)` | `test_a_crash_after_bundle_publication_recovers_the_runs_money_when_served` | Money recovered right after ingest, before the compatibility lookup; a served result of a run with durable money settles once with it |
| B P1 | Cancel then resume settles the same `run_id` twice | **Reproduced** | `['cancelled', 'complete']` | `test_cancel_then_resume_journals_exactly_one_monotonic_settlement` | `journal.append_invocation` keeps exactly one line per `invocation_id` (earlier line replaced, new one appended last) with monotonic money; every pipeline settlement merged with recovered money |
| B P1 | `failed` never becomes a `RunResult` | **Reproduced** | `RuntimeError: checkpoint store is broken` escaped `service.run` | `test_a_real_failure_after_paid_work_returns_a_failed_run_result` | `service.run` returns the recorded `failed` outcome (reason = redacted failure text) with settled spend; web runner and CLI surface that reason; queue worker hands it to retry/dead-letter |
| B P1 | Only CR/LF/NUL refused in `PlatformUrl` | **Reproduced** | `DID NOT RAISE TargetRefused` (`\x01`) | `test_every_c0_control_and_del_is_refused_in_a_platform_url` (all C0 + DEL, path and host; asserts the `urlsplit` tab-dropping differential) | Refuse any code point `< 0x20` or `0x7F` before any parsing |
| A P1 | Retryable zero-cost resolution treated as finished on resume | **Reproduced** | window 1 absent from resumed attempts `[4, 5, 6]` | `test_a_retryable_zero_cost_outcome_is_retried_after_a_crash` | `resume` → `retry` (fresh ordinal, parent = the zero-cost attempt); retries bounded per pass |
| A P1 | `--allow-degrade` ignores recovered spend | **Reproduced** | `('degraded', 'free')` | `test_allow_degrade_is_refused_once_the_run_has_cumulative_spend` | Decision uses cumulative `usd_admitter.usd_e6_spent` (includes restored spend) |
| A P1 | Interrupted secondary neither restored nor protected from same-run re-query | **Reproduced** (both halves) | closed breaker: `same-run no_match answers were sent again: [0]`; open breaker: `assert 0 >= 1` | `test_a_partial_secondary_is_not_re_queried_on_a_same_run_resume`, `test_a_partial_secondary_is_restored_even_when_the_breaker_is_open` | Same-run resume passes empty `refresh_states` to Shazam; with the breaker open, `recognise.restore_run_answers` rebuilds this run's own raw answers (no network, no job-store change) and re-fuses them before the refusal |
| A P1 | Local checkpoints prove existence, not durability; trusted without revalidation | **Reproduced** | `'decode'` still complete after its PCM was deleted | `test_a_local_checkpoint_whose_artefact_is_gone_or_changed_is_not_complete` | `service.durable_artefact_records` (flush + size + SHA-256) and `checkpoint_entry_valid` on every `completed_phases` read |
| B P2 | Direct pipeline entry points remain | Not in scope (P2) | — | — | Unchanged |
| B P2 | `RunResult.attempts` meaning inconsistent | Not in scope (P2) | — | — | Unchanged |

### Retro-review 4b-i

| Tag | Finding | Verdict | HEAD evidence | Regression test | Fix |
|---|---|---|---|---|---|
| A P0 | New per-run JSONL not namespace-durable; missing JSONL restores zero | **Reproduced** | JSONL deleted, SQLite kept: `assert 4 == (7 - 5)` (re-sent); `fsync_directory` never called for a new journal | `test_a_lost_jsonl_directory_entry_recovers_paid_state_from_sqlite`, `test_a_new_attempt_journal_is_namespace_durable` | `io.create_file_durably` (Windows `MoveFileExW` WRITE_THROUGH create-if-absent; POSIX link + directory fsync) + `ensure_directory_durable`; `SQLiteAttemptJournal.durable_events` folds validated SQLite rows with the JSONL |
| A P0 | Same-run recovery aliases a second request onto the first attempt id; duplicates silently ignored | **Reproduced** | self-parent and reused id accepted (`DID NOT RAISE ValueError`); conflicting duplicate `DID NOT RAISE` | `test_self_parenting_and_a_reused_attempt_id_are_refused`, `test_a_conflicting_duplicate_event_is_verified_not_ignored` (+ the service B P0 test) | Fresh ordinals via the shared resume rule; `_EventProjection.insert` verifies a conflicting `(attempt_id, seq)` payload → `LedgerConflict` |
| A P1 | Malformed `progress` JSON stops the consumer | **Reproduced** | `sqlite3.OperationalError: malformed JSON` | `test_malformed_waiting_progress_is_quarantined_and_the_consumer_carries_on` | `json_valid`-guarded `_ATTACHED_SQL` and reconciliation; the row reaches the existing transactional quarantine |
| A P1 | Cancelling an attached job is ignored | **Reproduced** | `assert 'complete' == 'cancelled'` | `test_cancelling_an_attached_job_detaches_it_and_reconciliation_keeps_it_cancelled` | `request_cancel` detaches an attached waiting job at once (`cancelled`, `detached=1`); reconciliation skips cancel-requested rows |
| A P1 | Hosted execution publishes `present/current` and the global index | **Reproduced** | `...present\current` written | `test_a_hosted_worker_never_writes_the_local_current_pointer_or_index` | `service.run` passes `presentation_local = store.mode == "local"` → `publish_result(local=...)` |
| B P0 | Lease expiry missing from the fences | **Reproduced** | expired-but-unreclaimed claim: checkpoint `DID NOT RAISE StaleClaim`; intake commit proven separately (below) | `test_an_expired_but_unreclaimed_lease_fences_every_run_side_write`, `test_an_expired_intake_claim_cannot_commit_its_intake` | `require_run_fence`: run token = active driving job's token with `lease_until > now`, inside the write transaction, before checkpoint, attempt `prepared`/`dispatched`, reservation; `fail`/`wait`/`terminal`/intake commit fence on active state + unexpired lease |
| B P0 | Accounting only preserved after a `primary` checkpoint | **Reproduced** | cancel: `assert 0 == (4 * 5000)`; dead letter: `assert 0 == (4 * 5000)` | `test_cancelling_a_job_that_died_before_primary_keeps_its_durable_spend`, `test_dead_lettering_a_job_that_died_before_primary_keeps_its_durable_spend` | `fold_run_money` (shared fold over SQLite events + `_reservation` + primary state + row, MAX) inside `terminal` and `_abandon_run`; cancel backfills the JSONL first |
| B P1 | Windows rename can leave an absent checkpointed artefact that is still trusted | **Reproduced** | `decode.py` publishes PCM with plain `os.replace` (inspection); SQLite `decode` still complete after its artefact was unlinked | `test_decoder_output_is_published_with_the_write_through_move`, `test_a_sqlite_checkpoint_whose_artefact_is_absent_is_invalidated` | `io.durable_replace` (flush source; Windows WRITE_THROUGH; POSIX directory fsync) in `decode`; SQLite store records and revalidates artefacts like the local store |
| B P1 | Backfill parses the whole JSONL inside `BEGIN IMMEDIATE` | **Reproduced** | writer lock held during parsing: `[True, True, True]` | `test_journal_backfill_parses_outside_the_write_transaction` | Read + parse outside any transaction, project in fenced batches of 64 |
| B P1 | Fresh breaker per job; Shazam `dispatched` recorded after I/O | **Reproduced** (all three) | `[None, None]`; supplied shared breaker → `0` ledger rows; states at send `['prepared']` | `test_one_breaker_state_spans_sequential_jobs_in_a_worker`, `test_a_supplied_shared_breaker_is_still_projected_into_the_ledger`, `test_shazam_dispatched_is_recorded_before_network_io` | `Worker.process_breaker` (one per process); `LedgerShazamBreaker` now delegates all policy to it; new `ShazamBreaker.sent()` hook, called by `ShazamAdapter` right before network I/O, records `dispatched`; attempt id carried in a `ContextVar` |

**Already fixed by 4a-ii:** none of these findings — every one reproduced at `ec32f97`.
**Not reproducible:** none.

## The ten invariants — enforcement and tests

1. **Never re-bill.** `RunLedger.resume`: `ambiguous` and `settled` are never dispatched; only
   `fresh`/`retry`/`reissue` are, with `next_ordinal = 1 + max(ordinal)`. `AttemptJournal.prepare`
   refuses self-parenting and a reused id; `fold_run_ledger` and `_EventProjection.insert` verify
   duplicates. Tests: service B P0 test; queue A P0 tests.
2. **Original reservation restored from its durable record.** The pipeline records the reservation
   (`record_reservation`, write-once, sidecar + SQLite `_reservation`) before the sweep; on resume
   it uses `ReservationRecord.reservation()` and never calls `reserve_usd`; spend is restored as the
   ledger's exact µUSD (`restore_spent`). Tests: the three B P0 reservation tests; the updated
   `test_a_lowered_cap_resumes_against_the_durable_reservation_and_keeps_its_spend`.
3. **Same-run money recovered before any early return.** `recovered` is computed immediately after
   ingest; `_settle()` merges it into every settlement — compatible serve, refusal, provider
   unavailable, waiting, cancel, failure, success. Test: the crash-after-publication test.
4. **One terminal settlement per `run_id`, monotonic.** Local: `append_invocation` replaces the run's
   line and takes the MAX of every money field. Queue: `terminal`/`_abandon_run` fold durable events
   first and write MAX; the fence clears the token, so a second settlement is refused. Tests: cancel
   then resume; cancel and dead-letter before `primary`.
5. **Every run-side write fenced by token + active state + unexpired lease.** `require_run_fence`
   (checkpoint, attempt events = dispatch admission, reservation, Shazam ledger rows) and the fenced
   `fail`/`wait`/`terminal`/`_commit_intake`. Tests: the two lease tests.
6. **Journal namespace-durable before provider I/O; SQLite recovery.** `append_line` creates the
   file with `create_file_durably` + directory fsync before the first event; `durable_events` merges
   SQLite rows. Tests: the two queue A P0 journal tests.
7. **Zero-cost retryable resolutions distinguished; `allow_degrade` on cumulative spend.** `retry`
   action; `already_billed` uses the admitter's cumulative spend. Tests: A P1 retry and degrade tests.
8. **Every real terminal failure returns the contracted `RunResult`.** `service.run` returns the
   recorded `failed` outcome with settled money. Test: B P1 failed test.
9. **Artefacts fsynced, hashed, revalidated; absent artefact invalidates.** `durable_artefact_records`
   and `checkpoint_entry_valid` shared by both stores; decoder uses `durable_replace`. The two fuse
   phases checkpoint the flat fuse working copies that every later re-fuse rewrites, and resume
   always re-fuses from restored evidence, so for `fuse1`/`fuse2` the bar is presence; every other
   phase is size + SHA-256. Tests: local and SQLite revalidation tests; decoder test.
10. **Remaining P1s.** Partial secondary (restore + refresh suppression), C0/DEL, malformed waiting
    rows, attached cancellation, one breaker per process with `dispatched` before I/O, decoder
    write-through, batched backfill, hosted never writes `present/current` — each with its test above.

## Existing tests changed (they encoded behaviour this cycle deliberately changes)

- `tests/test_service_api.py`: four crash call sites (`_crashed_deep_run`, the Free-primary,
  completed-secondary and degraded-resume tests) now assert a `failed` `RunResult` instead of an
  escaping `_Boom` (invariant 8). `test_a_lowered_cap_refuses_the_resume_without_erasing_its_spend`
  became `test_a_lowered_cap_resumes_against_the_durable_reservation_and_keeps_its_spend`
  (invariant 2); it also asserts a fresh run is still refused by the lowered cap. `_entries`
  docstring updated (one settlement per run).
- `tests/idea_web/test_worker.py`: new `_claim_run` helper — the append-only, backfill and Shazam
  ledger tests set a token on `analysis_runs` without a claimed job, which the lease-bound fence now
  (correctly) refuses; the reclaimed-claim test's replacement store gets the test's fake clock.

## Judgement calls the orchestrator should see

- **Lowered cap after a durable reservation no longer refuses the resume.** Invariant 2 forbids
  recomputing the reservation from current caps; the resume stays within the reservation already
  made. A fresh run is still refused by the lowered cap.
- **`invocations.jsonl` keeps one line per run.** A resumed run's earlier settlement line is
  replaced and the new one appended last (atomic rewrite, as before). Readers that take the last
  line (`shown_result_dir`, the failed-runs card, the corpus) keep working.
- **A same-run resume suppresses Shazam `refresh_states` for every recognise call of that run**
  (not only the secondary): its cached `no_match` answers are this run's own evidence.
- **The single-event journal append (one line + fsync) stays inside its fenced SQLite
  transaction**, so the fence check and the durable `dispatched` are atomic with respect to lease
  loss; only the unbounded backfill was moved out.
- **On Windows `fsync_directory` remains a no-op**; new-file durability uses WRITE_THROUGH moves.
- `RunResult.reason` for `failed` is the redacted failure text (the journal `reason` stays `null`).
- The P2 findings (direct pipeline entry points, `RunResult.attempts` semantics) are untouched.

## Files changed

- New: `src/id_detector/run_ledger.py`; `tests/test_followup_money_resume.py`;
  `tests/idea_web/test_followup_queue_money.py`; this record.
- Modified: `src/id_detector/{attempts,cli,decode,io,journal,money,paid_clip,pipeline,recognise,service,shazam,shazam_breaker}.py`,
  `src/id_detector/webapp/runner.py`, `src/idea_web/jobs/worker.py`,
  `tests/test_service_api.py`, `tests/idea_web/test_worker.py`.
- Not touched: `docs/PLAN-v2.md`, `profiles/`, `data/`, `work/`, playlists, `README.md`,
  `idea.cmd`, `present/theme.py`. No new dependency; no HTTP framework import in `id_detector` or
  `idea_web.jobs`.

## Reproduction outputs at `ec32f97` (before any production change)

`uv run pytest tests/test_followup_money_resume.py -q` (the 15th test only asserts the fixture
exists):

```text
FAILED tests/test_followup_money_resume.py::test_a_same_run_ambiguous_attempt_is_spent_and_never_sent_again
FAILED tests/test_followup_money_resume.py::test_resume_restores_the_original_reservation_and_price_not_todays
FAILED tests/test_followup_money_resume.py::test_a_lowered_cap_does_not_recompute_a_durable_reservation
FAILED tests/test_followup_money_resume.py::test_an_injected_admitter_on_resume_is_charged_with_the_recovered_spend
FAILED tests/test_followup_money_resume.py::test_a_crash_after_bundle_publication_recovers_the_runs_money_when_served
FAILED tests/test_followup_money_resume.py::test_cancel_then_resume_journals_exactly_one_monotonic_settlement
FAILED tests/test_followup_money_resume.py::test_a_real_failure_after_paid_work_returns_a_failed_run_result
FAILED tests/test_followup_money_resume.py::test_every_c0_control_and_del_is_refused_in_a_platform_url
FAILED tests/test_followup_money_resume.py::test_a_retryable_zero_cost_outcome_is_retried_after_a_crash
FAILED tests/test_followup_money_resume.py::test_allow_degrade_is_refused_once_the_run_has_cumulative_spend
FAILED tests/test_followup_money_resume.py::test_a_partial_secondary_is_not_re_queried_on_a_same_run_resume
FAILED tests/test_followup_money_resume.py::test_a_partial_secondary_is_restored_even_when_the_breaker_is_open
FAILED tests/test_followup_money_resume.py::test_a_local_checkpoint_whose_artefact_is_gone_or_changed_is_not_complete
FAILED tests/test_followup_money_resume.py::test_decoder_output_is_published_with_the_write_through_move
14 failed, 1 passed, 1 warning in 58.95s
```

Failure lines, in the same order:

```text
AssertionError: an attempt id was reused inside one run
AssertionError: assert 29400 == 36750
AssertionError: RunResult(run_id='cap-lowered', status='budget_exhausted', reason='reservation_exceeds_cap', achieved=None, bundle_id=None, usd_e6_reserved=0, usd_e6_spent=20000, attempts=0)
AssertionError: assert 15000 == (7 * 5000)
AssertionError: assert 0 == (7 * 5000)
AssertionError: ['cancelled', 'complete']
RuntimeError: checkpoint store is broken
Failed: DID NOT RAISE TargetRefused
AssertionError: [{'provider': 'audd', 'window': 4, ...}, {'provider': 'audd', 'window': 5, ...}, {'provider': 'audd', 'window': 6, ...}]
AssertionError: ('degraded', 'free')
AssertionError: same-run no_match answers were sent again: [0]
AssertionError: assert 0 >= 1
AssertionError: assert 'decode' not in frozenset({'decode', 'fuse1', 'fuse2', 'hints', 'ingest', 'present', ...})
assert 'os.replace(' not in '"""One-pass...'
```

Ambiguous-resume probe at `ec32f97` (same scenario as the first test, counts printed):

```text
audd_concurrency 4
pass1 dispatched 4 unresolved 1
resume calls 3 windows [2, 4, 5]
status partial spent 35000
prepared 8 unique 7
self-parent 3
```

The resume re-sent the ambiguous window 2, exhausted the reservation and left a never-sent window
unsent (`partial`); one attempt id was prepared twice and three events named themselves as parent.

`uv run pytest tests/idea_web/test_followup_queue_money.py -q`:

```text
FAILED tests/idea_web/test_followup_queue_money.py::test_a_lost_jsonl_directory_entry_recovers_paid_state_from_sqlite
FAILED tests/idea_web/test_followup_queue_money.py::test_a_new_attempt_journal_is_namespace_durable
FAILED tests/idea_web/test_followup_queue_money.py::test_self_parenting_and_a_reused_attempt_id_are_refused
FAILED tests/idea_web/test_followup_queue_money.py::test_a_conflicting_duplicate_event_is_verified_not_ignored
FAILED tests/idea_web/test_followup_queue_money.py::test_malformed_waiting_progress_is_quarantined_and_the_consumer_carries_on
FAILED tests/idea_web/test_followup_queue_money.py::test_cancelling_an_attached_job_detaches_it_and_reconciliation_keeps_it_cancelled
FAILED tests/idea_web/test_followup_queue_money.py::test_a_hosted_worker_never_writes_the_local_current_pointer_or_index
FAILED tests/idea_web/test_followup_queue_money.py::test_an_expired_but_unreclaimed_lease_fences_every_run_side_write
FAILED tests/idea_web/test_followup_queue_money.py::test_cancelling_a_job_that_died_before_primary_keeps_its_durable_spend
FAILED tests/idea_web/test_followup_queue_money.py::test_dead_lettering_a_job_that_died_before_primary_keeps_its_durable_spend
FAILED tests/idea_web/test_followup_queue_money.py::test_a_sqlite_checkpoint_whose_artefact_is_absent_is_invalidated
FAILED tests/idea_web/test_followup_queue_money.py::test_journal_backfill_parses_outside_the_write_transaction
FAILED tests/idea_web/test_followup_queue_money.py::test_one_breaker_state_spans_sequential_jobs_in_a_worker
FAILED tests/idea_web/test_followup_queue_money.py::test_a_supplied_shared_breaker_is_still_projected_into_the_ledger
FAILED tests/idea_web/test_followup_queue_money.py::test_shazam_dispatched_is_recorded_before_network_io
15 failed, 1 warning in 23.90s
```

Failure lines, in the same order:

```text
assert 4 == (7 - 5)
AssertionError: assert WindowsPath('.../fresh/attempts') in set()
Failed: DID NOT RAISE ValueError
Failed: DID NOT RAISE any of (ValueError, IntegrityError)
sqlite3.OperationalError: malformed JSON
AssertionError: assert 'complete' == 'cancelled'
AssertionError: assert ['\\\\?\\C:\\...ent\\current'] == []
Failed: DID NOT RAISE StaleClaim
assert 0 == (4 * 5000)
assert 0 == (4 * 5000)
AssertionError: assert 'decode' not in frozenset({'decode'})
AssertionError: [True, True, True]
AssertionError: assert [None, None] == [None, 'shaza...daily_budget']
assert 0 == 3
AssertionError: [['prepared']]
```

**D1 split.** The expired-lease test first failed at its checkpoint fence, so its intake-commit half
never executed at `ec32f97`. That half became `test_an_expired_intake_claim_cannot_commit_its_intake`
on a separate database (the first job's expired lease made it claimable again). With the fixed code
in place, only its one clause (`AND lease_until > ?`) was reverted to `AND ? IS NOT NULL` — HEAD's
behaviour — and the test run, then restored:

```text
                "AND ? IS NOT NULL",
E   Failed: DID NOT RAISE StaleClaim
1 failed, 1 warning in 2.91s
                "AND lease_until > ?",
1 passed, 1 warning in 2.52s
```

## Gate outputs (after the fixes)

Baseline for comparison — `uv run pytest -q` at `ec32f97` before any change:
`1286 passed, 1 skipped, 93 deselected, 5 warnings in 875.27s (0:14:35)`.

### 1. `uv run pytest -q`

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 1313 passed, 1 skipped, 93 deselected, 1 warning in 652.30s (0:10:52)
```

Exactly the four known environmental retention failures and nothing else. They also fail on `main`
on this machine: pytest's temp counter passed 999, which pushes their seeding paths past the
260-character limit (`LongPathsEnabled = 0`). A separate cycle is fixing that; they were not
changed here. 1313 passed against 1286 at baseline, which is the 31 new tests minus the four.

### 2. Named subset

`uv run pytest tests/test_service_api.py tests/idea_web/test_worker.py tests/idea_web/test_local_queue.py tests/idea_web/test_parity.py tests/idea_web/test_legacy_contract.py tests/test_phase0a_money.py tests/test_phase0a_status.py tests/test_phase0a_crash_cache.py tests/test_phase0b_attempts.py tests/test_phase0b_audd.py tests/test_phase1a_compat.py tests/test_phase1b_breaker_scorer.py tests/test_phase2b_retention.py tests/test_playlists.py tests/test_golden_local_free.py -q`:

```text
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 397 passed, 1 warning in 292.64s (0:04:52)
```

**These four are an environment artefact, not a regression.** All four fail inside the test's own
seeding helper `_seed_media` (untouched test code, no production call):
`downstream.parent.mkdir()` succeeds, then a plain `Path.write_text` on
`...\pytest-of-natha\pytest-1014\test_success_statuses_prune_wi0\pcm-old\<64 hex>\<64 hex>\recognise\observations.jsonl`
raises `FileNotFoundError` — the Windows 260-character `MAX_PATH`. pytest's temp counter on this
machine passed 1000 during this cycle (`pytest-99x` at baseline, `pytest-1014` here), lengthening
every temp path by one character and pushing these deepest seeded paths over the limit. Same file,
same code, a shorter base temp:

```text
uv run pytest tests/test_phase2b_retention.py -q --basetemp=C:/Users/natha/AppData/Local/Temp/idea-fu
31 passed, 1 warning in 20.25s
```

Every other file in the subset passes, including all 27 `test_worker.py` and 20 `test_service_api.py`.

### 3. `uv run ruff check .`

```text
All checks passed!
```

### 4. `uv run ruff format --check .`

```text
304 files already formatted
```

### 5. `uv run python scripts/audit_fixtures.py`

```text
audited 460 files
fixture audit passed
```

### 6. `uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

### 7–8. `scripts/smoke_serve.ps1` and `scripts/gate_local_mode.ps1` — NOT RUN

Both were invoked exactly as briefed
(`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`, and the same for
`gate_local_mode.ps1`). The worktree-isolation guard of this agent session refused every
`powershell` invocation before any process started — including a read-only
`Get-NetTCPConnection` port probe:

```text
This agent is isolated in the worktree C:\Users\natha\Documents\Music\id-detector\.claude\worktrees\agent-ac7462893536c29b5, but this command runs powershell in a plain command; what it reads or is handed as shell text cannot be shown not to run git. Refusing to run it — a worktree-isolated agent's git operations must target its own worktree.
```

This is neither a port collision nor a product failure: nothing was started. **Both gates still
have to be run by the orchestrator** before commit. They are the only check of the owner's
`idea.cmd` local-mode path in a real browser. The code on that path changed as follows:
- `webapp/runner.py` now raises the run's own failure text for a `failed` result.
- `service.run` and the pipeline changed underneath the runner.
- The local store now hashes checkpoint artefacts.

### 9. Repository state

`git status --short -- work data` — empty.

`git status --short`:

```text
 M src/id_detector/attempts.py
 M src/id_detector/cli.py
 M src/id_detector/decode.py
 M src/id_detector/io.py
 M src/id_detector/journal.py
 M src/id_detector/money.py
 M src/id_detector/paid_clip.py
 M src/id_detector/pipeline.py
 M src/id_detector/recognise.py
 M src/id_detector/service.py
 M src/id_detector/shazam.py
 M src/id_detector/shazam_breaker.py
 M src/id_detector/webapp/runner.py
 M src/idea_web/jobs/worker.py
 M tests/idea_web/test_worker.py
 M tests/test_service_api.py
?? docs/reviews/followup-money-resume.md
?? src/id_detector/run_ledger.py
?? tests/idea_web/test_followup_queue_money.py
?? tests/test_followup_money_resume.py
```

Processes: both background pytest runs this agent started completed (task notifications received). No `idea serve`, worker or uvicorn process was started, because the PowerShell gates never launched.

## Second-model review (sol xhigh) + fix pass

Review: `diff-review-followup-money-resume-sol.out.md` (Codex gpt-5.6-sol, xhigh, read-only),
verdict FIX_FIRST. Every finding was first written as a test and run against the pre-review-fix
code in the foreground; the outputs are quoted below.

### Correction: both PowerShell gates were run and passed

The review states the two PowerShell/browser gates were not independently verified. They were: the
orchestrator ran `scripts/smoke_serve.ps1` and `scripts/gate_local_mode.ps1` (the `idea.cmd`
double-click path) from this worktree against the pre-review code, and both passed with no process
left running. This session's worktree guard refuses PowerShell, so the orchestrator will re-run both
gates against the final code; this pass did not attempt them.

### Findings, verdicts, tests and fixes

| # | Finding | Verdict | Failing test → pre-fix evidence | Fix |
|---|---|---|---|---|
| P0-1 | Supervised local worker re-bills after a hard worker-process death: runner minted a new `run_id` per execution; cancel-before-restart settled `run_id=""`, zero money | **Reproduced** | `test_a_supervised_local_job_killed_mid_sweep_resumes_its_own_run_without_rebilling` and `test_a_price_change_between_local_crash_and_restart_keeps_the_original_reservation` → `the restart ran a different service run`; `test_cancelling_a_killed_local_job_before_restart_settles_its_durable_spend` → no settlement line at all | `webapp.jobs.Job.run_id`, minted at submit (`LocalJobs.submit`, `JobManager.submit`), snapshotted in the queue row, carried by `LocalWorker._restarted`; rows queued before the field get one persisted (fenced) BEFORE the run starts; runner uses `ctx.run_id`. Cancel/abandon before restart and every local settlement go through the shared `service.settle_interrupted_run` (ledger + durable reservation + primary checkpoint → one settlement line, exact µUSD onto the queue row and the job) |
| P0-2 | Malformed or de-marked attached job can abandon or rotate a live driver's run | **Reproduced** | `test_an_attached_job_can_never_abandon_or_take_over_a_live_drivers_run[malformed]` → run `dead_letter`; `[demarked]` → run token rotated (`None == <driver token>`); `test_a_second_driver_row_cannot_rotate_a_run_another_job_holds_a_live_lease_on` → token taken | Migration `0002_job_ownership` adds durable `jobs.attached` (backfilled from existing progress). Claims skip `attached = 1`; attach sets it; reconciliation and detach read the column, never progress JSON. `_rotate_run` hands a run's fence over only if no OTHER active, unexpired, non-attached job holds its token (otherwise the claim is returned untouched); `_abandon_run` (quarantine, dead letter) folds or unfences nothing while another job drives the run |
| P1-3 | Shazam `query_id` NULL in the ledger; unresolved Shazam dispatch resent on resume | **Partly reproduced.** NULL `query_id` reproduced. **The resend is not reproducible** | `test_hosted_shazam_ledger_rows_carry_the_clip_query_id` → NULL rows. `test_an_unresolved_shazam_dispatch_is_never_resent_on_a_same_run_resume` **passed against the pre-fix code** (same outcome locally and through the queue) | `shazam_breaker.SHAZAM_QUERY_ID` context variable, set by `recognise._run_job` around each query; `LedgerShazamBreaker` records it. Resend evidence: the per-media Shazam job store is itself §2.3.3's state machine. A query is `submission_started` before its request, and `lease_next` only leases `pending`/`retryable_failure`; neither `reset_for_refresh` nor the recognise cache loop resets `submission_started`. So a request killed mid-flight is never re-leased by any resume, local or queue. The test stays as a regression guard |
| P1-4 | Refresh suppression triggered by intake-only checkpoints (a regression of the first pass) | **Reproduced** | `test_a_fresh_run_with_intake_checkpoints_still_refreshes_cached_no_match` → `a fresh run reused another run's cached no_match` | Suppress only on real same-run evidence: recovered money, a run ledger entry, a checkpoint beyond `ingest`/`decode`, or this run's own Shazam invocation directory |
| P1-5 | Unhashed legacy checkpoints still trusted on existence | **Reproduced** | `test_an_unhashed_legacy_checkpoint_is_rebuilt_not_trusted` → `'decode' in frozenset({'decode'})` | `checkpoint_entry_valid` returns False without `artefact_records`; the phase is redone and re-checkpointed with hashes |
| P1-6 | Injected admitter checked on unit price only | **Reproduced** | `test_an_injected_admitter_must_hold_the_durable_reservation` → accepted; the run then crashed in fusion (`'NoneType' object has no attribute 'relative_to'`) | `restore_admitter` requires the whole reservation to equal the durable record (`LedgerConflict` otherwise → `failed`, no dispatch). **Also fixed, found by this test:** a resume whose recovered spend already fills the reservation skipped the sweep and left no primary observation file, so fusion crashed. The sweep now runs: it reuses resolved clips, its exhausted admitter refuses new dispatch, and the run ends `partial` |
| P1-7 | Duplicate verification omitted `ordinal` (fold) and `egress_id` (SQLite) | **Reproduced** | `test_a_conflicting_duplicate_ordinal_is_refused_by_the_shared_fold` → `DID NOT RAISE LedgerConflict`; `test_a_duplicate_projection_attributed_to_another_egress_is_refused` → `DID NOT RAISE ValueError` | One comparison, `AttemptEvent.conflicts`, for both stores: the payload plus `ordinal` and `egress_id` whenever both copies carry them. SQLite rows now read `egress_id`. A backfill replays a JSONL line, which has no egress, so it keeps the projected row's own attribution (`verify_egress=False`) |
| P1-8 | Zero-money compatible resume left the old terminal line | **Reproduced** | `test_a_zero_money_compatible_resume_replaces_the_runs_earlier_settlement` → `['cancelled'] == ['complete']` | The compatible branch settles whenever `journal.has_invocation(run_id)` is true, at any money |
| P1-9 | Ambiguous-attempt test not load-bearing; missing regressions | **Accepted** | Strengthened `test_a_same_run_ambiguous_attempt_is_spent_and_never_sent_again`: exact query identities (no first-pass or ambiguous clip re-sent; every never-dispatched clip sent once) and `reason != "reservation_exhausted"`. The missing supervised-local, corrupt-attached and Shazam-ambiguity regressions are the tests above | See the reversion demonstration below |

One judgement on the ambiguous test: after the fix the honest status of that run is `partial` /
`primary_not_achieved`, because the ambiguous clip has no answer. The assertion therefore forbids
the old failure mode (`reservation_exhausted`) rather than requiring `complete`.

### Reversion demonstration (strengthened ambiguous test)

Only `paid_clip._process`'s `if resume.action in {"settled", "ambiguous"}:` was changed to
`{"settled"}`, so an ambiguous clip falls through to a re-send. The test was run, then the line
restored and confirmed (`grep -c` = 1 before and after):

```text
E   AssertionError: RunResult(run_id='ambiguous-same-run', status='partial', reason='reservation_exhausted', ...)
1 failed, 1 warning in 7.38s
```

### Pre-fix outputs of the new regressions

Service seam (`-k` over the new tests plus the strengthened one):

```text
FAILED tests/test_followup_money_resume.py::test_a_same_run_ambiguous_attempt_is_spent_and_never_sent_again
FAILED tests/test_followup_money_resume.py::test_an_unhashed_legacy_checkpoint_is_rebuilt_not_trusted
FAILED tests/test_followup_money_resume.py::test_an_injected_admitter_must_hold_the_durable_reservation
FAILED tests/test_followup_money_resume.py::test_a_zero_money_compatible_resume_replaces_the_runs_earlier_settlement
FAILED tests/test_followup_money_resume.py::test_a_conflicting_duplicate_ordinal_is_refused_by_the_shared_fold
FAILED tests/test_followup_money_resume.py::test_a_fresh_run_with_intake_checkpoints_still_refreshes_cached_no_match
6 failed, 1 passed, 14 deselected, 1 warning in 18.84s
```

(The one pass was `test_an_unresolved_shazam_dispatch_is_never_resent_on_a_same_run_resume` — the
evidence for P1-3's non-reproducible half.)

`tests/idea_web/test_followup_review_fixes.py`:

```text
FAILED tests/idea_web/test_followup_review_fixes.py::test_a_supervised_local_job_killed_mid_sweep_resumes_its_own_run_without_rebilling
FAILED tests/idea_web/test_followup_review_fixes.py::test_a_price_change_between_local_crash_and_restart_keeps_the_original_reservation
FAILED tests/idea_web/test_followup_review_fixes.py::test_cancelling_a_killed_local_job_before_restart_settles_its_durable_spend
FAILED tests/idea_web/test_followup_review_fixes.py::test_an_attached_job_can_never_abandon_or_take_over_a_live_drivers_run[malformed]
FAILED tests/idea_web/test_followup_review_fixes.py::test_an_attached_job_can_never_abandon_or_take_over_a_live_drivers_run[demarked]
FAILED tests/idea_web/test_followup_review_fixes.py::test_a_second_driver_row_cannot_rotate_a_run_another_job_holds_a_live_lease_on
FAILED tests/idea_web/test_followup_review_fixes.py::test_hosted_shazam_ledger_rows_carry_the_clip_query_id
FAILED tests/idea_web/test_followup_review_fixes.py::test_a_duplicate_projection_attributed_to_another_egress_is_refused
8 failed, 1 warning in 24.58s
```

The supervised-local tests kill a real `idea_web.jobs.local` worker process with `os._exit(9)`
immediately after an AudD `dispatched` line. They then resume through the production
`make_pipeline_runner`, which gained keyword-only offline provider overrides that default to
`None`, so production runs unchanged.

### Test changes in this pass

- `tests/idea_web/test_worker.py`: the migration test now expects schema version 2.
- `tests/idea_web/test_followup_queue_money.py`: the attached-cancel unit test also sets the new
  `attached` column.
- `tests/idea_web/local_runner_fakes.py`: new subprocess runner factories (`deep_runner`,
  `fake_pipeline_runner`, `ExitAfterDispatch`).
- New file `tests/idea_web/test_followup_review_fixes.py`, and the new cases in
  `tests/test_followup_money_resume.py`.

### Gate outputs (final code, all in the foreground)

`uv run pytest -q` was run as two foreground shards covering every test file, because the whole
suite exceeds one foreground call's 10-minute limit:

```text
shard A (tests/idea_web + half of tests/test_*.py):
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 776 passed, 12 deselected, 1 warning in 513.79s (0:08:33)

shard B (the other half):
551 passed, 1 skipped, 81 deselected, 1 warning in 220.76s (0:03:40)
```

Total: 1,327 passed, 1 skipped, and **exactly the four known environmental retention failures and
nothing else**. They come from pytest's temp counter passing 999 with `LongPathsEnabled = 0`; they
also fail on `main` and are being fixed separately. They were not changed, and `--basetemp` was
not shortened.

Named subset (`tests/test_service_api.py … tests/test_golden_local_free.py`, as listed in the
instructions):

```text
407 passed, 1 warning in 426.66s (0:07:06)
```

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 305 files already formatted
uv run python scripts/audit_fixtures.py → audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  → page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data → (empty)
```

Processes: a read-only `psutil` scan for any `pytest`, local-worker, uvicorn or serve process whose
command line names this worktree printed `leftover processes: none`. The killed worker processes in
the supervised-local tests are reaped by `subprocess.run`; nothing was killed by image name.

Merge constraints respected: `src/id_detector/jobs.py`, `retention.py`,
`tests/test_phase2b_retention.py`, the truth-fix files, the second session's files and
`tests/test_phase0a_security.py` are untouched. `cli.py` is unchanged in this pass. No dependency
was added, and no HTTP framework import was added under `src/id_detector/` or `src/idea_web/jobs/`.

## Round-2 review (sol xhigh) + fix pass

Review: `diff-review-followup-money-resume-sol-r2.out.md` (Codex gpt-5.6-sol, xhigh, read-only),
verdict FIX_FIRST. Confirmed fixed by that review and left alone: the fresh-run Shazam refresh, the
legacy-checkpoint rebuild, full admitter-reservation equality and the zero-money compatible resume.
Both PowerShell gates passed on the pre-round-2 code when the orchestrator ran them; it re-runs
them on this code (this session's worktree guard refuses PowerShell).

Every finding below was first written as a test and run against the code as it stood before this
pass, in the foreground. The two coverage findings (P1-6) already passed there by construction;
they were proven load-bearing by reverting exactly the fix each pins (outputs below).

### Findings, verdicts, tests and fixes

| # | Finding | Verdict | Failing test → evidence before this pass | Fix |
|---|---|---|---|---|
| P0-1 | Supervised local path can lose real spend: `jobs.run_id` NULL, so a max-attempt quarantine settles nothing; a death before the first 0.5 s flush leaves no `started_at`, so cancel reports zero | **Reproduced** | `test_a_submitted_local_job_carries_its_run_id_in_the_queue_row` → `assert (None is not None)`; `test_three_worker_deaths_dead_letter_the_job_and_settle_its_durable_spend` → no settlement line (`expected 1, got 0`); `test_cancelling_after_a_death_before_the_first_progress_flush_settles_spend` → no settlement line | `LocalJobs.submit` passes the job's `run_id` to `JobQueue.enqueue` (normalised `jobs.run_id`); `cancel_unclaimed` treats "no run" as "no `analysis_runs` row", so a queued local job still cancels at once. `JobQueue(on_abandon=…)`: every quarantine (claim) and dead letter (`fail`) is settled through the hook only AFTER its transaction commits (a rolled-back quarantine settles nothing); `LocalWorker._settle_abandoned` → shared `service.settle_interrupted_run(..., status="failed")`. `LocalWorker._run_money` no longer requires `started_at`, and adopts `row.run_id` when the snapshot lacks one |
| P0-2 | `make_pipeline_runner` accepts the offline provider/config/sleep overrides without test-mode authorisation | **Reproduced** | `test_the_production_runner_refuses_offline_overrides_outside_test_mode[paid_scan_adapters\|shazam_http_client\|paid_sleep\|configure]` → `DID NOT RAISE PermissionError` (4 cases) | Any override outside `IDEA_TEST_MODE=1` raises `PermissionError` naming it; construction without overrides is unchanged (asserted) |
| P1-3 | Shazam ambiguity recovery not implemented; the round-1 "resend not reproducible" verdict was too broad | **Reproduced — round-1 verdict withdrawn.** The per-media job store protected the query only while it survived | Local: `test_an_unresolved_shazam_dispatch_is_never_resent_on_a_same_run_resume`, now deleting `jobs.sqlite*` → killed window re-requested (`[5, 6, 0, 1, 3, 2, ...]`). Queue: `test_a_hosted_worker_recovers_shazam_ambiguity_from_sqlite_after_losing_the_job_store` → re-requested (`[5, 0, 6, 1, 2, 3, ...]`) | `recognise` journals every Shazam request per run label in `recognise/shazam-attempts.jsonl` through `AttemptJournal`: `prepared`, then `dispatched` inside `on_attempt` before network I/O, then `resolved` with the Shazam outcome, carrying the clip `query_id`, fresh ordinals and parents. On resume a query whose newest attempt is dispatched-unresolved is excluded from leasing, from this JSONL or from the hosted SQLite ledger (`pipeline` folds `journal.events_for("shazam")` into `ambiguous_query_ids`). An answer this run already received, whose job-store row was lost, is served from the run's own raw body instead of asked again |
| P1-4 | Migration 0002 strands previously cancelled attached jobs | **Reproduced** | `test_migration_0002_on_a_populated_0001_database_backfills_detaches_and_reverses` → `assert 'waiting' == 'cancelled'` | 0002 (unshipped, edited in place) terminally detaches `attached = 1 AND cancel_requested = 1` active rows (`cancelled`, `detached=1`, claim cleared). The test populates a real 0001 database with in-flight, breaker-waiting, attached, attached-cancelled, terminal, dead-letter and malformed-progress rows. It proves: a 0002 that fails part-way leaves the database logically unchanged at version 1 (schema version and every row equal; the SQLite file's bytes are not compared — corrected in round 4); the backfill and detach; every other row's state unchanged; and `down` removes the column. A cancellation is deliberately not undone by `down` |
| P1-5 | JSONL and SQLite still built from two payloads | **Reproduced** | `test_one_event_object_feeds_both_the_jsonl_and_the_sqlite_projection` → JSONL `at` ≠ SQLite `at`; `test_a_second_real_writer_cannot_reuse_an_attempt_under_another_egress` → a second JSONL line written (`assert 2 == 1`); `test_a_journal_line_whose_ordinal_disagrees_with_its_attempt_id_is_refused` → `DID NOT RAISE LedgerConflict` | `AttemptJournal._write` builds ONE `ProviderAttemptEvent`; `_persist(record)` appends it. `SQLiteAttemptJournal._persist(record)`: fence → project that same object (verified, including `ordinal` and `egress_id`) → append it, in one transaction, so a conflicting duplicate is refused before anything reaches the JSONL. `prepare` refuses an attempt id already durable in the store (a second real writer). `event_from_record` refuses a line whose ordinal does not reproduce its attempt id |
| P1-6 | Missing load-bearing coverage: a real `LocalWorkerSupervisor` kill/restart; a matching-reservation full-spend resume | **Accepted** (coverage) | `test_a_paid_job_survives_a_worker_kill_through_the_real_supervisor` (real supervisor, the worker process `os._exit(9)`s after a paid dispatch, the supervisor restarts it, the replacement reclaims after the 30 s lease and finishes); `test_a_resume_whose_durable_reservation_is_already_spent_ends_partial_not_failed` (the durable reservation record is fully covered by the run's own spend) | Reversion demonstrations below |

### Reversion demonstrations (P1-6)

`revert_demos.py` replaced exactly one fix, ran the pinning test, then restored the file and
verified it byte-identical:

```text
== reverted: runner run-id reuse (supervisor kill/restart test)
E   ValueError: too many values to unpack (expected 1)
FAILED tests/idea_web/test_followup_round2.py::test_a_paid_job_survives_a_worker_kill_through_the_real_supervisor
1 failed, 1 warning in 39.51s
   exit=1 restored_byte_identical=True
== reverted: over-cap resume sweep (full-spend test)
      At index 0 diff: 'failed' != 'partial'
FAILED tests/test_followup_money_resume.py::test_a_resume_whose_durable_reservation_is_already_spent_ends_partial_not_failed
1 failed, 1 warning in 3.92s
   exit=1 restored_byte_identical=True
```

### Pre-fix outputs (before this pass)

```text
FAILED tests/idea_web/test_followup_round2.py::test_the_production_runner_refuses_offline_overrides_outside_test_mode[paid_scan_adapters]
FAILED tests/idea_web/test_followup_round2.py::test_the_production_runner_refuses_offline_overrides_outside_test_mode[shazam_http_client]
FAILED tests/idea_web/test_followup_round2.py::test_the_production_runner_refuses_offline_overrides_outside_test_mode[paid_sleep]
FAILED tests/idea_web/test_followup_round2.py::test_the_production_runner_refuses_offline_overrides_outside_test_mode[configure]
FAILED tests/idea_web/test_followup_round2.py::test_a_submitted_local_job_carries_its_run_id_in_the_queue_row
FAILED tests/idea_web/test_followup_round2.py::test_three_worker_deaths_dead_letter_the_job_and_settle_its_durable_spend
FAILED tests/idea_web/test_followup_round2.py::test_cancelling_after_a_death_before_the_first_progress_flush_settles_spend
FAILED tests/idea_web/test_followup_round2.py::test_a_hosted_worker_recovers_shazam_ambiguity_from_sqlite_after_losing_the_job_store
FAILED tests/idea_web/test_followup_round2.py::test_migration_0002_on_a_populated_0001_database_backfills_detaches_and_reverses
FAILED tests/idea_web/test_followup_round2.py::test_one_event_object_feeds_both_the_jsonl_and_the_sqlite_projection
FAILED tests/idea_web/test_followup_round2.py::test_a_second_real_writer_cannot_reuse_an_attempt_under_another_egress
FAILED tests/idea_web/test_followup_round2.py::test_a_journal_line_whose_ordinal_disagrees_with_its_attempt_id_is_refused
12 failed, 1 passed, 1 warning in 58.26s

FAILED tests/test_followup_money_resume.py::test_an_unresolved_shazam_dispatch_is_never_resent_on_a_same_run_resume
1 failed, 1 passed, 20 deselected, 1 warning in 8.72s
```

(The one pass in each run is a P1-6 coverage test.)

### Other changes in this pass

- `make_pipeline_runner` is gated as described. `tests/idea_web/local_runner_fakes.py` gains an
  `IDEA_FOLLOWUP_KILL_ONCE` marker so a supervisor-restarted worker survives.
- **Line endings:** this pass's patch scripts had written nine source/test files with CRLF
  (`Path.write_text` on Windows). The repository's `.gitattributes` sets `eol=lf`, so commits were
  never affected. Every modified and new text file was rewritten to LF, and
  `git ls-files --eol` shows `i/lf w/lf`.

### Residual risks

1. **Old local rows.** A local job queued before this change has no `jobs.run_id`. If that job dies
   three times, the quarantine hook cannot settle it (it has no run id to settle). `LocalWorker`
   mints and persists a run id in the snapshot on its first claim, but not in the column.
   **To hit it:** a job queued on the old code, still unfinished at upgrade, killed three times.
   **Closing it** needs a one-time column backfill from the snapshot's `run_id`.
2. **Settlement is best-effort after commit.** The `on_abandon` hook runs after the quarantine or
   dead letter commits, and its exceptions are suppressed so the queue survives. **To hit it:** a
   crash between that commit and the hook, or `_load_cached` failing for the media. The run then
   has no `failed` settlement line, although its ledger still holds the spend; the next
   `settle_interrupted_run` for that run id would record it. **Closing it** needs a sweep that
   settles dead-lettered runs that have no settlement line.
3. **Shazam rows written before round 1** carry `query_id = NULL` and cannot identify their query.
   **To hit it:** a hosted run interrupted mid-Shazam under the pre-fix code, resumed after upgrade
   *and* after losing its per-media job store. Hosted mode is not deployed, so no such rows exist
   today.
4. **Windows directory durability.** POSIX fsyncs the directory. Windows relies on WRITE_THROUGH
   create/replace for the JSONL, the reservation record and the decoder output; a file appended to
   later is fsynced, but its directory is not (Windows has no directory fsync).
5. **Settlement lines written without a pipeline pass** (`settle_interrupted_run`) carry the
   default `requested_recipe_id` (Free) and `counts={"paid_attempts": n}`. Their money is exact; the
   recipe label is not. **Closing it** needs the recipe persisted with the durable reservation.
6. **The pre-first-flush cancellation test** simulates the race by restoring the submission-time
   snapshot after a real worker death. That is exactly the on-disk state of a worker that died
   before its first flush, but the 0.5 s race itself is not timed.

### Gate outputs (final code, all in the foreground)

`uv run pytest -q` as two shards covering every collected test file (one run exceeds the 10-minute
foreground limit):

```text
shard A (tests/idea_web + half of tests/test_*.py):
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 790 passed, 12 deselected, 1 warning in 594.86s (0:09:54)

shard B (the other half):
551 passed, 1 skipped, 81 deselected, 1 warning in 236.17s (0:03:56)
```

Total: 1,341 passed, 1 skipped, and **exactly the four known environmental retention failures and
nothing else** (pytest's temp counter past 999, `LongPathsEnabled = 0`; being fixed separately;
not changed, `--basetemp` not shortened).

Named subset (the 17 files listed in the instructions, in two foreground halves):

```text
half 1 (test_service_api, test_followup_money_resume, test_followup_queue_money, test_followup_review_fixes, test_worker, test_phase0a_money, test_phase0a_status, test_phase0a_crash_cache):
154 passed, 1 warning in 272.99s (0:04:32)
half 2 (test_local_queue, test_parity, test_legacy_contract, test_phase0b_attempts, test_phase0b_audd, test_phase1a_compat, test_phase1b_breaker_scorer, test_playlists, test_golden_local_free):
262 passed, 1 warning in 155.94s (0:02:35)
```

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 306 files already formatted
uv run python scripts/audit_fixtures.py → audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  → page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data → (empty)
leftover processes (psutil scan, command lines naming this worktree) → none
```

Constraints: `src/id_detector/jobs.py`, `retention.py`, `tests/test_phase2b_retention.py`, the truth
files, the second session's files and `tests/test_phase0a_security.py` are untouched. `cli.py` is
unchanged in this pass. No dependency and no HTTP framework import was added under
`src/id_detector/` or `src/idea_web/jobs/`. Nothing is committed.

## Round-4 class-closing design pass (after the round-3 sol xhigh review)

Round 3 (`diff-review-followup-money-resume-sol-r3.out.md`) returned FIX_FIRST; the brief
(`money-round4-brief.md`) asked for each class to be closed by design rather than per instance.
Threat model: accidents, crashes, power loss, restarts, two tabs and ordinary cancellation are in
scope; hostile same-user programs are out of scope for code changes.

Method: all six new tests in `tests/idea_web/test_followup_round4.py` were written first and run
against the code as it stood before this pass. **All six failed**, each on its finding (below). The
same patch then made all six pass. Four existing tests encoded the replaced dual Shazam writer and
were rewritten (listed below).

### Item 1 — ONE run id: minted in one place, backfilled, never read from progress JSON

- **Design.** `new_run_id()` is the one code helper that mints (corrected in round 5: migration 0002's SQL backfill also mints, and job-driven paths never mint). Submission writes the id into the
  `jobs.run_id` column and the snapshot in the same INSERT. `JobQueue.adopt_run_id` returns the
  column's id; a claimed row that somehow has none gets one minted and written to the column and
  the snapshot in one fenced transaction. Migration 0002 (unshipped, edited in place) backfills every
  live local row: the snapshot's `$.local.run_id` when it is text, otherwise a fresh id, then sets the
  snapshot to the column's value. All JSON extraction is guarded by `json_valid`. `job_view` takes
  `run_id` from the column only.
- **Code.** `src/idea_web/jobs/worker.py:135` (`new_run_id`), `:1198` (`adopt_run_id`);
  `src/idea_web/jobs/local.py:145` (`job_view` reads the column), `:214` (submission),
  `:461` (claim adopts); `src/idea_web/migrations/0002_job_ownership.up.sql:20-37` (backfill).
- **Test.** `test_an_upgraded_0001_local_row_gets_one_run_id_and_three_deaths_still_settle`: a real
  0001 database with a pre-run-id local row, migrated by the next start, then three real worker
  deaths and quarantine. Before this pass → `AssertionError: the upgrade left the normalised run id
  NULL`.

### Item 2 — settlement is DERIVED by a startup + periodic sweep

- **Design.** `LocalWorker.sweep_settlements` runs when the worker starts and every
  `SWEEP_SECONDS` (120 s). It settles every `dead_letter` / `failed` / `cancelled` /
  `provider_unavailable` job whose run holds spend but has **no** settlement line, through
  `service.settle_interrupted_run(..., only_unsettled=True)`. That call is idempotent: a run that
  already has its line is never touched, so a sweep can never replace a pipeline's own settlement.
  The post-commit `on_abandon` callback stays only as a fast path; the sweep is the guarantee.
- **Code.** `src/idea_web/jobs/local.py:77` (`SWEEP_SECONDS`, `_SWEEP_STATUS`), `:391`
  (`sweep_settlements`), `:423` (`run_forever` sweeps first, then periodically);
  `src/idea_web/jobs/worker.py:1224` (`settlement_candidates`); `src/id_detector/service.py:552,590-592`
  (`only_unsettled`).
- **Test.** `test_a_death_right_after_the_dead_letter_commit_is_settled_once_by_the_next_start`: a
  real child process whose settlement callback is `os._exit(9)` dies the instant the dead-letter
  transaction commits. The next start settles it; a second start leaves exactly the same single line.
  Before this pass → `ValueError: not enough values to unpack (expected 1, got 0)` (never settled).

### Item 3 — ONE dispatch-admission check, including `cancel_requested = 0`, hosted and local

- **Design.** `DispatchAdmission` holds the single SQL check `_JOB_CLAIM_SQL + cancel_requested = 0`.
  It requires: the matching claim token, an active state, an unexpired lease, the run's token when
  the job drives a queue-side run, and no cancellation request. It is evaluated immediately before
  the `dispatched` event is durably written:
  - **Hosted:** inside the SQLite journal's own write transaction, atomic with the projection and
    append.
  - **Supervised local:** through `AttemptJournal.admission` immediately before the JSONL append.
    `LocalWorker` attaches the guard per execution, and the runner forwards it as
    `PipelineOptions.dispatch_admission`. The local path no longer relies on the unfenced store or
    the 0.5 s progress relay.

  A refusal (`attempts.DispatchRefused`) is raised before anything is written. The admitted unit is
  refunded, the attempt stays `prepared`, and the sweep cancels.
- **Code.** `src/idea_web/jobs/worker.py:144` (`_JOB_CLAIM_SQL`), `:154` (`DispatchAdmission`), `:759`
  (hosted check in `_persist`); `src/id_detector/attempts.py:37` (`DispatchRefused`), `:171` (local
  check before append); `src/idea_web/jobs/local.py:497` (guard attached per execution);
  `src/id_detector/paid_clip.py:516-528` (refund and stop); `src/id_detector/pipeline.py:567`.
- **Tests.** Each commits the cancellation from inside the adapter, after the sweep's last halt check
  and before AudD's admission callback — deterministic, no sleeps:
  - `test_a_cancel_committed_before_audd_admission_stops_the_hosted_request`. Before this pass →
    `assert 3 == 2` (one more dispatch went out).
  - `test_a_cancel_committed_before_audd_admission_stops_the_local_request`, with a 30 s progress
    flush so only the admission check can stop it. Before this pass → `assert 7 == 2`.

### Item 4 — terminal settlement goes through the same fence before any `invocations.jsonl` write

- **Design.** Every terminal settlement write in the pipeline (8 sites: waiting, compatible serve,
  budget refusal, provider unavailable, success, source changed, cancelled, failed) goes through
  `_append_settlement`, which calls the settlement fence first:
  - **Hosted:** `SQLiteCheckpointStore.fence_settlement` (the run fence).
  - **Local:** the same `DispatchAdmission` with `require_not_cancelled=False`, because a cancelled
    job must still settle. The local cancel settlement (`settle_interrupted_run(fence=...)`) uses the
    same fence.

  A stale worker's fence raises; nothing is written.
- **Code.** `src/id_detector/pipeline.py:468` (`_append_settlement`) and its eight call sites;
  `src/idea_web/jobs/worker.py:473` (`fence_settlement`); `src/id_detector/service.py:674,730`;
  `src/idea_web/jobs/local.py:500`.
- **Test.** `test_a_worker_whose_lease_was_reclaimed_writes_no_invocation_settlement`: the lease lapses
  and a replacement claims the job right after the `present` checkpoint, immediately before the
  settlement. Before this pass → `AssertionError: a stale worker wrote the run's settlement`.

### Item 5 — ONE Shazam attempt identity; recovery consumes every SQLite state

- **Design.** A Shazam request's only identity is the journal's own event: a deterministic attempt
  id over the service run and the clip query id. The pipeline obtains it as
  `journal.for_provider("shazam")`: locally the per-media `recognise/shazam-attempts.jsonl`; hosted,
  a `SQLiteAttemptJournal` projecting that same object into `provider_attempt_events`.
  `LedgerShazamBreaker` no longer writes rows; it only delegates policy to the worker's one process
  breaker. On resume, recognise consumes the whole fold (JSONL plus SQLite) and never re-sends:
  - `ambiguous` (dispatched, unresolved) and `settled` (a terminal refusal) queries
  - an answered `reuse` query, which is re-served from its raw body, or not asked again when that
    body is absent
- **Code.** `src/id_detector/attempts.py:103` and `src/idea_web/jobs/worker.py:729`
  (`for_provider`); `src/idea_web/jobs/worker.py:776` (breaker delegates only);
  `src/id_detector/pipeline.py:863`; `src/id_detector/recognise.py:417,470` (`attempt_journal`,
  `_never_resend`).
- **Test.** `test_hosted_shazam_recovers_every_state_from_sqlite_with_store_and_journals_gone`: real
  writers, a kill mid-request, then the per-media job store and every Shazam JSONL deleted. Only
  SQLite survives; window 0 is a 401 refusal. Before this pass → `answered, refused or in-flight
  Shazam queries were sent again: {0, 5, 6}`.
- **Existing tests rewritten for the single identity:**
  - `test_worker.py::test_shazam_attempts_reach_the_ledger_and_are_fenced` now writes through the real
    journal. It asserts the query id on every row, that the breaker adds no rows, and that a stale
    claim is refused.
  - `test_followup_queue_money.py::test_a_supplied_shared_breaker_is_the_workers_one_policy_state`
    (renamed from `…_is_still_projected_into_the_ledger`) asserts the supplied breaker is the policy
    and that zero breaker rows are written.
  - `test_followup_queue_money.py::test_shazam_dispatched_is_recorded_before_network_io` now runs a
    real hosted Free job and, at every send, finds that exact query's `dispatched` row.
  - `test_followup_round2.py::test_one_event_object_feeds_both_the_jsonl_and_the_sqlite_projection`
    no longer patches a worker timestamp: the one object carries its own `at`.

### Item 6 — record correction

The round-2 row for the migration test claimed a failed 0002 leaves the database "byte-for-byte
unchanged". The test proves schema version and logical row equality, not file bytes. It now reads
"logically unchanged (schema version and every row equal; the SQLite file's bytes are not
compared)".

### Adversarial money risks — awaiting the owner's decision

Round 3 raised two `[adversarial, money]` risks. They are **awaiting the owner's decision**. They
are out of scope for this pass's code changes and are not accepted by this record. Closing either
would need cross-process exclusive attempt creation and an external or tamper-evident ledger.

1. Two hostile same-user processes that deliberately bypass the media lock can race attempt creation
   and dispatch one clip twice.
2. A hostile same-user process can delete or coherently alter both the JSONL ledger and its SQLite
   projection or reservation before a resume, erasing recorded spend.

### Residual risks (realistic)

- **Sweep reach.** The sweep settles stopped jobs whose run id is in the column. Terminal rows from
  before durable run ids (not active at migration) are not backfilled; under the old code their spend
  was keyed to random per-execution ids, so no run id could recover them anyway.
- **Confirmation pass after store loss.** A reused Shazam answer whose raw body lives only in a
  different invocation directory (a confirmation pass over identical audio) is not asked again after
  the job store is lost. That window is then reported unanswered rather than re-sent. This is a
  deliberate never-resend bias; no money is involved.
- **Fence timing.** Local admission and settlement fences are checked immediately before the JSONL
  or journal write, not inside one transaction with the file. The hosted dispatch check is atomic
  with its SQLite projection.
- **Unchanged from earlier rounds.** The Windows directory-fsync limitation, and out-of-pipeline
  settlement lines defaulting the recipe label to Free (money exact).

### Gate outputs (final code, all in the foreground)

`uv run pytest -q` as two shards covering every collected test file:

```text
shard A (tests/idea_web + part of tests/test_*.py):
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
4 failed, 754 passed, 12 deselected, 1 warning in 346.07s (0:05:46)

shard B (the rest, including test_service_api.py and test_followup_money_resume.py):
593 passed, 1 skipped, 81 deselected, 1 warning in 284.39s (0:04:44)
```

Total: 1,347 passed, 1 skipped, and **exactly the four known environmental retention failures and
nothing else** (pytest temp counter past 999 with `LongPathsEnabled = 0`; fixed separately, not
changed).

```text
round-4 tests before this pass: 6 failed, 1 warning in 21.62s
round-4 tests after:            6 passed, 1 warning in 22.51s
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 307 files already formatted
uv run python scripts/audit_fixtures.py → audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  → page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data → (empty)
leftover processes (psutil scan, command lines naming this worktree) → none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Round-5 pass: SQLite is the single money authority (after the round-4 sol xhigh review)

Round 4 (`diff-review-followup-money-resume-sol-r4.out.md`) returned FIX_FIRST. The coordinator
asked for one design change closing realistic items 2, 3 and 5 as a single class: **SQLite is the
single authority** for paid dispatch and terminal settlement, for hosted and supervised-local runs
alike. Items 1 and 4 were fixed alongside it.

**Migration choice:** a **new migration, `0003_money_authority`** (`run_dispatches`, `run_settlements`).
0002's shape is unchanged.

Method: `tests/idea_web/test_followup_round5.py` was written first and run against the code as it
stood before this pass. **7 of its 8 tests failed**, each on its finding; the 8th only checks a helper
exists. After the pass all 8 pass.

### Items 2, 3, 5 — one class: SQLite rows first, files are projections

- **Design, dispatch.** `DispatchAdmission.admit_in` runs the claim check (claim token, active
  state, unexpired lease, run token, `cancel_requested = 0`) and inserts the authoritative
  `run_dispatches` row **in one transaction**. The primary key `(run_id, attempt_id)` makes a
  duplicate impossible: a second insert is a no-op, and a no-op is a refusal (`DispatchRefused`), so
  nothing is sent.
  - **Supervised local:** `AttemptJournal._persist` calls `admission.admit` and appends the JSONL
    line only after that commit.
  - **Hosted:** `SQLiteAttemptJournal._persist` calls `admit_in` inside its own transaction and
    appends after commit.
  - **Recovery:** both fold the rows back in (`durable_events` merges `dispatched_events`), so a
    committed dispatch whose JSONL projection was lost is still spent and never re-sent. Nothing may
    send a request unless its row committed.
- **Design, settlement.** `SettlementLedger` owns `run_settlements`, primary key `run_id`:
  - The claim holder's writer upserts the row under its fence in one transaction, with monotonic
    money: hosted via `SQLiteCheckpointStore.settlement_writer` and the run fence; local via the job
    claim check.
  - `only_if_missing` writers (the derived sweep, the post-commit fast path) insert only when no row
    exists, so concurrent writers produce exactly one row.
  - After commit the row is projected into `invocations.jsonl`, and `reproject` restores a lost line.
  - All 8 pipeline settlement sites and the local cancel settlement use it.
- **Design, sweep.** `LocalWorker.sweep_settlements` decides "missing settlement" by the SQLite row
  and covers **every** terminal state, `complete` included. For each job:
  - a row exists → re-project its line if missing
  - no row, but a pre-round-5 journal line exists → adopt it
  - otherwise → settle what the durable ledger proves was spent
  - a recovery miss (media not locatable: `SettlementMiss`) → retried by later sweeps (up to 3 per
    process, and again at every start), never marked done
- **Code.**
  - `src/idea_web/migrations/0003_money_authority.up.sql`
  - `src/idea_web/jobs/worker.py:186` (`admit_in`), `:215` (`admit`), `:224` (`dispatch_events`),
    `:236` (`SettlementLedger`: `settle` `:263`, `adopt` `:306`, `reproject` `:329`), `:631`
    (`settlement_writer`), `:923` (hosted admission inside the transaction), `:938` (JSONL after
    commit)
  - `src/id_detector/attempts.py:103,179-180`
  - `src/idea_web/jobs/local.py:81` (`_SWEEP_STATUS`, every terminal state), `:94`, `:414`
    (`sweep_settlements`), `:443` (`_settle_run`), `:597` (local settlement writer)
  - `src/id_detector/service.py:545` (`SettlementMiss`), `:549` (`settlement_line`), `:572`
    (`settle_interrupted_run`), `:702`
  - `src/id_detector/pipeline.py:471` (`_append_settlement`)
- **Tests.**
  - `test_admission_is_one_transaction_so_a_racing_cancel_or_reclaim_never_lets_a_request_leave[cancel|reclaim]`
    covers three phases:
    1. Cancel or reclaim committed from a second connection before admission: refused, no line, no
       request.
    2. Committed the instant admission returns, before the JSONL line: the dispatch must already be
       durable. Before this pass → `a request was authorised before its dispatch was durable`.
    3. Attempted **while admission holds the transaction** (a barrier seam inside it): the racer's
       `BEGIN IMMEDIATE` is blocked until the row commits, and every later dispatch is refused with
       no further request.
  - `test_two_concurrent_settlement_writers_produce_exactly_one_row_and_one_line`: the sweep and the
    dead-letter fast path race behind a barrier. Before this pass → `no such table: run_settlements`.
  - `test_a_sweep_restores_a_complete_runs_missing_settlement_line_from_its_row`. Before this pass →
    the `complete` run's line was never restored.
  - `test_a_recovery_miss_is_retried_by_the_next_sweep`. Before this pass → the first miss marked the
    run swept forever.

### Item 1 — pre-upgrade terminal local jobs with NULL `jobs.run_id`

- **Design.** At startup (and each sweep), `_recover_unidentified` reads
  `JobQueue.unidentified_terminal_rows`: stopped local jobs with no run id, each with the job's own
  time window. For each job it locates the media and parses the attempt ledger. Every run id whose
  events fall inside the window (±5 s) is settled **exactly once** through `_settle_run`, the same
  unique `run_settlements` row. A job whose media or run is missed is retried.
- **Code.** `src/idea_web/jobs/worker.py:1407`; `src/idea_web/jobs/local.py:119` (`_epoch`), `:467`
  (`_recover_unidentified`).
- **Test.** `test_pre_upgrade_terminal_jobs_without_a_run_id_are_settled_once_from_their_ledger`: a
  populated 0001 database with a cancelled, NULL-run-id local job whose real paid run was killed
  mid-primary. Two starts produce one line and one row. Before this pass → no settlement.

### Item 4 — job-driven paths never mint; one code helper

- **Design.** Job-driven code fails closed:
  - `webapp.runner` raises `RuntimeError("this job has no durable run id; …")` before any analysis.
  - `pipeline.run_analysis` raises `ValueError("a job-driven analysis must carry its durable run id")`
    when a dispatch admission or settlement writer is present without a run id.

  Every legitimate code mint goes through `run_ledger.new_run_id()`: the CLI (`cli.py:696-698`), the
  legacy in-memory job manager (`webapp/jobs.py:573`), local submission and adoption, and hosted
  intake (`worker.py:2040`). The pipeline fallback is used only by non-job callers.
- **Record wording corrected.** The round-4 section called `new_run_id()` "the ONLY minting
  function". Accurately: it is the one code helper; migration 0002's SQL backfill is the only other
  mint; job-driven paths never mint and fail closed.
- **Code.** `src/id_detector/run_ledger.py:74`; `src/id_detector/webapp/runner.py:318`;
  `src/id_detector/pipeline.py:443`.
- **Test.** `test_a_job_driven_run_without_a_durable_run_id_fails_closed`: the real runner with a
  job context lacking a run id, and the pipeline with a settlement writer and no run id. Before this
  pass → `DID NOT RAISE RuntimeError`, and the analysis started under a fallback id.

### Existing tests updated

- Schema-version expectations moved to 3: `tests/idea_web/test_worker.py` (version, concurrent
  migrators, applied count) and `tests/idea_web/test_followup_round2.py` (`migrate() == 3`).
- Five web-runner tests passed a duck-typed job context **without a run id** and relied on the old
  fail-open fallback mint. Each context now carries a run id, as every real job does; nothing else
  in them changed:
  - `test_phase1a_compat.py::test_web_runner_passes_recipe_and_delivers_selected_bundle[None-free|max_accuracy-deep]`
  - `test_phase1a_compat.py::test_web_acquisition_republishes_the_selected_bundle`
  - `test_phase1a_cached_open.py::test_web_runner_publishes_bundle_result_url`
  - `test_phase1b_breaker_scorer.py::test_web_runner_owns_one_breaker_across_jobs_and_reports_waiting`

### Adversarial money risks — still awaiting the owner's decision

Unchanged and not widened: a lock-bypassing hostile same-user process racing attempt creation, and
one deleting or altering both ledgers. Out of scope for code changes; not accepted by this record.

### Residual risks (realistic)

- **Pre-upgrade window heuristic.** Two pre-upgrade jobs on the same mix with overlapping windows
  could attribute a run to the wrong job row. Each run is still settled exactly once, with exact
  money; only `run_settlements.job_id` could name the neighbour.
- **Projection lag.** `invocations.jsonl` can briefly lack a line whose row committed (a crash
  between commit and projection); the next sweep restores it. Readers that need certainty should
  read `run_settlements`.
- **Miss retry bound.** A run whose media is permanently gone is retried up to 3 times per process
  and again at every start — bounded, never marked done.
- **Unchanged from earlier rounds.** No Windows directory fsync; settlement lines written outside a
  pipeline pass carry the default recipe label, with exact money.

### Gate outputs (final code, all in the foreground)

`uv run pytest -q` as two shards covering every collected test file:

```text
shard A (tests/idea_web + part of tests/test_*.py), first run:
FAILED tests/test_phase1a_compat.py::test_web_runner_passes_recipe_and_delivers_selected_bundle[None-free]
FAILED tests/test_phase1a_compat.py::test_web_runner_passes_recipe_and_delivers_selected_bundle[max_accuracy-deep]
FAILED tests/test_phase1a_compat.py::test_web_acquisition_republishes_the_selected_bundle
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[degraded]
FAILED tests/test_phase2b_retention.py::test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[partial]
FAILED tests/test_phase2b_retention.py::test_reparse_point_cannot_move_files_outside_work
7 failed, 759 passed, 12 deselected, 1 warning in 334.68s (0:05:34)

shard B (the rest), first run:
FAILED tests/test_phase1a_cached_open.py::test_web_runner_publishes_bundle_result_url
FAILED tests/test_phase1b_breaker_scorer.py::test_web_runner_owns_one_breaker_across_jobs_and_reports_waiting
2 failed, 591 passed, 1 skipped, 81 deselected, 1 warning in 275.47s (0:04:35)
```

The five non-retention failures were the fail-closed runner refusing duck-typed contexts with no
run id (`Actual message: 'this job has no durable run id; its analysis was not started'`). After
giving those contexts a run id:

```text
5 passed, 1 warning in 13.17s
```

Net: 1,350 passed, 1 skipped, and **exactly the four known environmental retention failures and
nothing else** (pytest temp counter past 999 with `LongPathsEnabled = 0`; fixed separately, not
changed).

```text
round-5 tests before this pass: 7 failed, 1 passed, 1 warning in 26.48s
round-5 tests after:            8 passed, 1 warning in 15.63s
related suites after (round 4, round 2, review fixes, queue money, worker, local queue,
  money resume, service API):  121 passed, 1 warning in 227.39s (0:03:47)
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 308 files already formatted
uv run python scripts/audit_fixtures.py → audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  → page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data → (empty)
leftover processes (psutil scan, command lines naming this worktree) → none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Round-6 pass: local money authority closed, hosted paid dispatch scoped out (after the round-5 sol xhigh review)

The round-5 review came back FIX_FIRST (P0-1 local reservation sidecar-only and unfenced, P0-2
hosted cancel/dead-letter without settlement rows, P0-3 first-line legacy adoption, P0-4 populated
0003 downgrade, P1-5 zero-money terminal paths and hosted reprojection). Owner priority is
local-first and hosted mode has never been deployed, so this pass closes the local findings and
removes hosted paid reachability instead of extending hosted money code.

Tests first: `tests/idea_web/test_followup_round6.py` (16 cases) was run against the round-5 code
before any production change:

```text
round-6 tests before this pass: 13 failed, 3 passed, 1 warning in 27.25s
round-6 tests after:            16 passed, 1 warning in 23.37s
```

The three that already passed pre-fix are regression coverage, not reproductions: the hosted free
Shazam run, the empty downgrade, and the "missing line" half of the reprojection test (round 5
already restored a missing line; the "differs" half failed).

### A — hosted paid dispatch fails closed (closes finding 2 and the hosted half of finding 5)

Design: removal of reachability, not new hosted money code. `HOSTED_PAID_REFUSAL = "paid engines
are not enabled in hosted mode yet"`; a recipe is paid when its cap is non-zero or its primary or
secondary engine is a paid scanner/clip engine (`recipe_uses_paid_engine`).

- Intake: the hosted `Worker.run_once` (queue path, `local_mode=False`) refuses a paid recipe right
  after the cancel check and before intake, any `analysis_runs` row, reservation or dispatch, via
  `JobQueue.fail(..., permanent=True)` -> `dead_letter` with that reason
  (`src/idea_web/jobs/worker.py:1941`, `:1651`).
- Dispatch admission: `DispatchAdmission(hosted=True).check` and `.reserve` raise
  `DispatchRefused(HOSTED_PAID_REFUSAL)` (`worker.py:199`, `:252`); `SQLiteAttemptJournal` now takes
  `hosted: bool = True` (fail closed by default, `worker.py:924`) and refuses every non-Shazam event
  before any transaction (`worker.py:1074`) and every reservation (`worker.py:1022`). The worker
  builds it with `hosted=not self.local_mode` (`worker.py:2399`); `for_provider` propagates it.
- Free Shazam hosted runs are unaffected (the Shazam journal is exempt).
- Tests: `test_a_hosted_paid_request_is_refused_at_intake_before_any_reservation_or_dispatch`
  (dead letter with the message, 0 AudD calls, and `analysis_runs`, `provider_attempt_events`,
  `run_dispatches`, `run_reservations`, `run_settlements` all empty),
  `test_hosted_dispatch_admission_itself_refuses_a_paid_engine`,
  `test_a_hosted_free_shazam_run_still_completes`.

**Hosted paid money is deferred to the hosted cycle.** The hosted `SettlementLedger` code, the
hosted `analysis_runs` reservation (`SQLiteAttemptJournal.durable_reservation` still prefers the
sidecar), the hosted cancel/dead-letter settlement rows and a hosted sweep were deliberately NOT
extended. All of it must be re-reviewed before hosted paid engines are enabled; removing the
refusal alone would reopen round-5 findings 2 and 5.

### B — the local reservation lives in SQLite (closes finding 1)

Migration choice: **0003 edited in place** (it has never been committed or shipped; schema version
stays 3). `0003_money_authority.up.sql:36` adds `run_reservations(run_id PRIMARY KEY, job_id,
reservation, usd_e6_reserved, created_at)`.

- `DispatchAdmission.reserve` (`worker.py:243`): ONE transaction checks the claim (`_JOB_CLAIM_SQL`:
  token, active state, unexpired lease), checks the job drives that run, and inserts the unique row;
  an existing row always wins. A stale worker raises `StaleClaim` and writes nothing.
  `stored_reservation` (`worker.py:291`) reads it; an unreadable row raises `LedgerConflict`.
- `AttemptJournal` (`src/id_detector/attempts.py:131-190`): with an authority attached,
  `durable_reservation` reads SQLite ONLY (the sidecar is never read back),
  `record_reservation` writes SQLite first and then mirrors it with `project_reservation`
  (`run_ledger.py:484`, overwrites a stale or forged file). The direct CLI keeps the file.
- Fail closed (`pipeline.py:793`): when `dispatch_without_reservation()` (`attempts.py:155`) proves
  a dispatch with no reservation row, the run settles `failed` / `reservation_missing` with its
  folded spend and makes no further paid request; it never recomputes from today's price or cap.
- Local settlement reads the SQLite reservation first too (`settle_interrupted_run(reservation=)`,
  `local.py:670`).
- Tests: `test_a_stale_worker_writes_no_reservation_and_a_price_change_never_alters_it` (worker A
  reserves, lapses, B reclaims at 2x price: A's later write raises and changes nothing, B restores
  A's reservation, a forged sidecar is ignored; a worker lapsed before reserving writes no row and no
  sidecar), `test_a_resumed_local_run_restores_its_sqlite_reservation_not_the_sidecar_or_todays_price`
  (real worker process killed mid-sweep, sidecars deleted, resumed at 2x price),
  `test_dispatch_rows_without_a_reservation_row_fail_closed_and_never_recompute` (0 AudD calls).

### C — legacy adoption reconciles every line (closes finding 3)

- `settlement_lines` (`service.py:572`) returns EVERY line for the run (`journal.invocation_lines`,
  `journal.py:100`).
- `SettlementLedger.adopt(run_id, path, lines, money)` (`worker.py:398`): the newest valid line names
  the outcome; every money field is the maximum over all lines AND the durable fold (attempt JSONL,
  `run_dispatches`, `run_reservations`, primary checkpoint); `paid_attempts` likewise. One canonical
  row is inserted, then the projection is rewritten to exactly one line.
- The sweep (`local.py:445`) adopts only when no row exists.
- Test: `test_legacy_lower_cancelled_then_higher_complete_lines_adopt_one_maximal_row_and_line`
  (cancelled line with the higher reservation, complete line with the higher spend -> one
  `complete` row with both maxima, exactly one line, idempotent on a second start).

### D — a populated downgrade fails closed (closes finding 4)

`0003_money_authority.down.sql` creates a TEMP guard table with a `BEFORE INSERT` trigger that
`RAISE(ABORT, 'refusing to downgrade: the money authority tables hold rows')` when
`run_dispatches`, `run_settlements` or `run_reservations` holds any row, then inserts a probe. The
runner executes the script inside `BEGIN IMMEDIATE` and rolls back on error, so nothing is dropped
and `schema_migrations` still says 3. Empty tables: the guard passes and all three tables drop.

- Tests: `test_a_populated_0003_downgrade_is_refused_and_the_rows_survive[run_dispatches|
  run_settlements|run_reservations]`, `test_an_empty_0003_downgrade_still_works`.

### E — every local job-owned terminal run gets a row (closes the local half of finding 5)

- `settle_interrupted_run` always writes when a settlement writer is given, zero money included
  (`service.py:656`); `interrupted_entry` (`service.py:583`) is the one entry builder.
- The pipeline's compatible-result branch settles whenever a settlement writer is attached
  (`pipeline.py:679`).
- Media never located: `LocalWorker._provably_unspent` (`local.py:674`) is true only for a job
  created after migration 3 was applied with no `run_dispatches` and no `run_reservations` row (every
  local paid request commits both first). Such a run gets a zero row with an empty `journal_path`
  (nothing to project into) from `_run_money` (`local.py:722`) or the sweep (`local.py:461`). Older
  jobs remain a retried miss.
- `SettlementLedger.reproject` (`worker.py:446`) acts when the line is missing, differs from the row,
  or is duplicated. A line reporting MORE money first raises the row (monotonic, never under-report);
  otherwise the row wins, and the file ends with exactly one line.
- Tests: `test_a_zero_money_local_cancellation_gets_a_settlement_row[claimed|unclaimed]`,
  `test_a_zero_money_compatible_return_gets_a_settlement_row`,
  `test_the_sweep_reprojects_a_line_that_is_missing_or_differs_from_its_row[missing|differs]`.

### Existing tests updated

Six direct `SQLiteAttemptJournal(provider="audd")` constructions now pass `hosted=False` (they test
the shared queue journal's fence and projection, i.e. the queue's local mode):
`test_followup_queue_money.py` (duplicate-event verification, expired-lease fence),
`test_followup_review_fixes.py` (egress duplicate), `test_followup_round2.py` (one event object,
second writer), `test_worker.py` (append-only events). Their first-run failure was exactly
`DispatchRefused: paid engines are not enabled in hosted mode yet`. No assertion was changed.

### Adversarial money risks — still awaiting the owner's decision

Unchanged and not widened by this pass.

### Residual risks (realistic)

- Hosted paid money (above) is deferred; the refusal is the only thing keeping it safe.
- `Worker(local_mode=True)` (the queue path in local mode) is constructed only by tests, never by
  `idea serve`; its reservation stays in `analysis_runs.checkpoints` plus the sidecar.
- A pre-upgrade local run whose attempt JSONL proves dispatches but has no reservation row now fails
  closed (`reservation_missing`) instead of resuming paid work.
- A job created before migration 3 whose media cannot be located stays a retried miss (3 per
  process) and has no row until its media is found.
- The direct CLI path (no SQLite) keeps the reservation file as its authority, as before.

### Gate outputs (final code, all in the foreground)

```text
first full run (literal file lists):
shard A (tests/idea_web + top-level a-o and phase*): 10 failed, 703 passed, 1 warning in 518.91s
  -> the 4 known retention failures + the 6 hosted-refusal constructions above
shard B (the rest):                                  662 passed, 1 skipped, 93 deselected, 1 warning in 116.91s

after the six constructions pass hosted=False, shard A re-run as two halves:
tests/idea_web:                                      183 passed, 1 warning in 169.62s
top-level a-o and phase*:                            4 failed, 526 passed, 1 warning in 356.28s
  -> exactly the four known environmental retention failures
     (test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d[complete|degraded|partial],
      test_reparse_point_cannot_move_files_outside_work)

Net: 1,371 passed, 1 skipped, and only the four known retention failures.
uv run ruff check .          -> All checks passed!
uv run ruff format --check . -> 309 files already formatted
uv run python scripts/audit_fixtures.py -> audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  -> page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data -> (empty)
leftover processes (psutil scan, command lines naming this worktree) -> none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Round-7 pass: settlement without media, durable provenance, retries, Shazam fence (after the round-6 sol xhigh review)

The round-6 review came back FIX_FIRST with two P0s (media-missing runs ignore exact SQLite spend;
"provably unspent" relied on wall-clock ordering), two P1s (three-miss retry suppression;
supervised-local Shazam journals dropped the job fence), a P1 test gap, and P2s (dispatch and
settlement fences not bound to `jobs.run_id`; stale comments). This pass closes all of them.

Tests first: `tests/idea_web/test_followup_round7.py` (13 cases) against the round-6 code gave
11 failed, 2 passed. The 2 that passed are the hosted analysis-resume and waiting-retry refusals
(round-6 behaviour, added as regression coverage). After the pass: 13 passed. Every case was also
shown to fail with its own fix reverted in the round-7 code
(`scratchpad/revert_round7_demos.py`, which restores the original bytes after each run):

```text
R1 settlement from SQLite when media is missing: 1 failed
R2 durable provenance stamp (zero row without proof): 2 failed
R3 migration refused while the supervisor lock is held: 1 failed
R4 schema check inside the claim transaction: 1 failed
R5 retries never dropped (three-miss suppression restored): 1 failed
R6 Shazam provider journal inherits the claim fence: 1 failed
R7 hosted refusal at claim (matrix): 4 failed
R8 paid-engine names count even at a zero cap: 1 failed
R9 dispatch binds the job's run: 1 failed
R9b paid dispatch requires the run's reservation row: 1 failed
R10 settlement binds the job's run: 1 failed
all reverted fixes fail their tests
```

Migration choice: **0003 edited in place again** (never committed or shipped; schema version stays 3).

### Item 1 (P0) — settlement without media folds SQLite first

- `LocalWorker._settle_run` (`src/idea_web/jobs/local.py:516-545`): on `SettlementMiss` it calls
  `_authority_money` (`local.py:757`). Any paid `run_dispatches` or `run_reservations` row gives the
  conservative fold: every dispatch without its resolution counts as spent, and the reservation is
  the row's own. That settlement row is written with an empty `journal_path` (no projection). A run
  with no authority rows and no provenance stamp stays a miss and waits for its media.
- Once the media is found, `SettlementLedger.attach` (`src/idea_web/jobs/worker.py:491`) sets the
  path, raises money to the full fold (never lowers it), and reprojects exactly one line. A row that
  holds money stays scheduled until then (`_row_holds_money`, `local.py:127`).
- `_run_money` (the claim-holder's fenced settlement) uses the same `_authority_money` on a miss.
- Test: `test_paid_media_missing_settles_exactly_from_sqlite_and_projects_once_it_returns`. A real
  worker process is killed mid-sweep, the job is stopped, and media lookup fails: the row holds
  spend = dispatched x unit and reservation = the row. With the media back: path set, same money,
  exactly one line.

### Item 2 (P0) — durable provenance instead of timestamps

- The created_at-vs-applied_at heuristic is removed.
- 0003 up adds `jobs.money_authority` and `jobs.authority_token`, plus trigger
  `jobs_money_authority_provenance` (`0003_money_authority.up.sql:49-58`): any claim-token change
  whose `authority_token` does not equal the new `claim_token` sets `money_authority = 0` for good.
  Older code knows neither column, so its claim always trips the trigger. 0003 down drops the
  trigger and columns, behind the same populated-table guard.
- `JobQueue.enqueue` stamps `money_authority = MONEY_AUTHORITY (3)` in the submit INSERT
  (`worker.py:1304`, constant `:79`). `_claim` writes `authority_token = claim_token` and keeps 3
  only if already 3 (or unset on a never-claimed row) in the same UPDATE (`worker.py:1479-1490`).
- A zero row is written only when `money_authority == 3` and there are no paid dispatch or
  reservation rows (`local.py:757`). Unstamped or 0-marked jobs never get a zero row.
- `local_database` (`local.py:165`) refuses to migrate while another supervisor holds
  `worker-supervisor.lock`, raising `MigrationRefused`: "ID'er needs to upgrade this work folder's
  database, but another ID'er is still running analyses here: stop the running ID'er first, then
  start this one again". `idea serve` prints it and exits 2 (`src/id_detector/cli.py:893`, minimal
  change).
- `_claim` reads `MAX(schema_migrations.number)` inside the claim transaction and raises
  `SchemaTooNew` when it is newer than this code's migrations (`worker.py:1459`). `LocalWorker`
  exits cleanly (`local.py:622`, exit code `EXIT_SCHEMA_TOO_NEW = 3`), and the supervisor stops
  restarting it and says why (`local.py:1030`).
- Tests: `test_an_old_worker_claim_marks_the_job_unproven_so_it_never_gets_a_zero_row`,
  `test_clock_skew_on_an_unstamped_job_never_yields_a_zero_row`,
  `test_serve_refuses_to_migrate_while_another_supervised_worker_holds_the_lock`,
  `test_a_worker_exits_cleanly_when_the_schema_is_newer_than_its_code`.

### Item 3 (P1) — misses stay scheduled

- `MAX_RECOVERY_MISSES` is removed. `sweep_settlements` keeps `run_id -> (misses, next due)`
  (`local.py:499`). `retry_delay_seconds` (`local.py:157`) retries at every sweep for the first 3
  misses, then backs off at 240 s, 480 s ... capped at `MAX_RETRY_SECONDS = 1800`
  (`local.py:99-100`), for as long as the worker runs. A new start begins with an empty schedule,
  so it retries at once.
- Test: `test_a_settlement_is_recovered_after_more_than_three_misses` (legacy JSONL-only spend,
  six misses, backoff observed, then settled exactly with one line once the media returns).

### Item 4 (P1) — supervised-local Shazam fence

- `AttemptJournal.for_provider` now propagates the parent's admission (`src/id_detector/attempts.py:121`),
  so every Shazam `dispatched` event runs `DispatchAdmission.admit_in` (claim, lease,
  `cancel_requested = 0`) and inserts its `run_dispatches` row under the `(run_id, attempt_id)`
  primary key before the request leaves. A second writer of that identity is refused.
- `recognise.py:362` turns a refused Shazam dispatch into cancellation rather than a recorded
  failure. Money readers take paid rows only (`dispatch_events(..., paid_only=True)`,
  `worker.py:336`; `durable_events` filters by provider, `attempts.py:107`).
- Test: `test_a_stale_or_duplicate_supervised_local_shazam_dispatch_is_refused` (a reclaimed worker
  is refused with no row; two writers of one identity let exactly one through; a committed cancel
  refuses).

### Item 5 (P1) — regressions

Items 1-4 tests above, plus the hosted refusal matrix (all refused at `Worker.run_once` before
intake or service, no attempt/dispatch/reservation rows):
`test_hosted_refusal_on_an_analysis_state_resume`, `test_hosted_refusal_on_a_waiting_retry`,
`test_hosted_refusal_on_a_legacy_or_attached_row`,
`test_hosted_refusal_on_a_zero_cap_recipe_naming_a_paid_engine`.

### Item 6 (P2) — binding and comments

- `DispatchAdmission.admit_in` (`worker.py:238-247`), inside the dispatch transaction: the record's
  `run_id` must equal `jobs.run_id`, and a non-Shazam dispatch needs its `run_reservations` row.
  Queue local mode `SQLiteAttemptJournal.record_reservation` now writes that row too
  (`worker.py:1141`).
- `SettlementLedger.settle` (`worker.py:403`), inside the settlement transaction: an entry for a
  run other than the job's `run_id` raises `LedgerConflict`.
- Comments fixed: the `attempts.py` module header (ambiguous attempts are never resent; SQLite is
  the authority), the `SQLiteAttemptJournal` docstring (JSONL is a projection), and the round-6 test
  module header (13 of 16 failed; 3 were regression coverage).
- Tests: `test_paid_dispatch_requires_the_jobs_own_run_and_its_reservation`,
  `test_a_fenced_settlement_refuses_an_entry_for_another_run`.

### Existing tests updated

- `test_followup_round5.py::_journal` records the run's reservation first, as the pipeline does
  before its first dispatch (required by item 6).
- `test_followup_round6.py` counts paid dispatch rows (`provider='audd'`) where it counted every
  dispatch row, since Shazam identities are now fenced there too. No assertion was weakened.

### Adversarial money risks — still awaiting the owner's decision

Unchanged and not widened.

### Residual risks (realistic)

- Hosted paid money stays deferred behind the round-6 refusal.
- A job with no stamp (pre-0003, or claimed by older code) and missing media waits for its media
  indefinitely. It is retried with backoff, never dropped, but it gets no row until then.
- An old worker that was already running when the database was upgraded by a process that did not
  go through `local_database` (e.g. a manual `Database.migrate`) could still claim. The trigger
  marks those jobs unproven, so the worst case is a waiting settlement, never a zero row.
- Queue `Worker(local_mode=True)` remains test-only.

### Gate outputs (final code, all in the foreground)

```text
round-7 tests before this pass: 11 failed, 2 passed
round-7 tests after:            13 passed, 1 warning in 10.86s
round-5 + round-6 after:        24 passed, 1 warning in 38.11s

tests/idea_web:                 196 passed, 1 warning in 188.77s
top-level a-o and phase*:       4 failed, 526 passed, 1 warning in 325.34s
  -> exactly the four known environmental retention failures
shard B (the rest):             662 passed, 1 skipped, 93 deselected, 1 warning in 119.75s

Net: 1,384 passed, 1 skipped, and only the four known retention failures.
uv run ruff check .          -> All checks passed!
uv run ruff format --check . -> 310 files already formatted
uv run python scripts/audit_fixtures.py -> audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  -> page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data -> (empty)
leftover processes (psutil scan, command lines naming this worktree) -> none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Round-8 pass: migrations cannot bypass the lock; exact settlement ownership (after the round-7 sol xhigh review)

The round-7 review came back FIX_FIRST with one realistic P0 (the migration exclusion lived only in
`local_database`, so a direct `Database.migrate()` bypassed it) and two P2s (settlement accepted a
missing or null-run owner job; a stale pathless-settlement docstring). This pass is deliberately
small and refactors nothing else.

Tests first: `tests/idea_web/test_followup_round8.py` (5 cases) against the round-7 code gave
5 failed. After the pass: 5 passed. Each fix was also reverted in turn in the round-8 code
(`scratchpad/revert_round8_demos.py`, original bytes restored after each run):

```text
R1 migrate takes the supervisor lock itself: 2 failed
R2 migrate refuses a live unstamped claim: 1 failed
R3 settlement needs an existing owner job: 1 failed
R4 settlement needs jobs.run_id to equal the run (null owner): 1 failed
R5 legacy recovery validates its association: 1 failed
all reverted fixes fail their tests
```

### Item 1 (P0) — migrations cannot bypass the lock

- The exclusion now lives in `Database.migrate()` itself (`src/idea_web/database.py:200`). When a
  migration is actually pending (up or down) on a local work-root database
  (`<work root>/.idea/app.db`, `is_local_work_root`, `database.py:232`), `_exclude_local_workers`
  (`database.py:235`) must first:
  - take `<work root>/.idea/worker-supervisor.lock` non-blockingly (`database.py:249`), the same
    lock every `idea serve` supervisor holds; and
  - prove no job holds an unexpired claim made without this code's authority token
    (`database.py:266`; before 0003 has added `authority_token`, any unexpired claim counts).
  Otherwise it raises `MigrationRefused` (`database.py:27`) with "stop the running ID'er first",
  releasing the lock. The lock is held for the whole migration and released afterwards.
- Hosted databases (any other path) are unaffected. A database already at its target takes no lock,
  so the supervised worker process, started while its supervisor holds the lock, still opens it.
- `local_database` (`src/idea_web/jobs/local.py:162`) now just calls `database.migrate()`;
  `MigrationRefused` is re-exported there for `idea serve` (`local.py:59`, unchanged CLI handling).
- **What provides the exclusion, stated plainly.** Code that shipped before schema 3 cannot be
  changed, and this pass does not pretend to simulate running it. The exclusion comes from two
  things only:
  1. the supervisor lock, which every shipped `idea serve` already takes while it supervises a
     worker, so no upgrade happens while an old supervisor is alive; and
  2. the claim check, which refuses while any unexpired claim lacks this code's authority token,
     so a still-leased claim from an old worker process blocks the upgrade until its lease expires.
  In practice an old `idea serve` that is still running also holds `idea.exe` open, which blocks
  `uv` (and `idea.cmd`, which runs through `uv`) from installing the upgraded code at all.
- The round-7 residual risk "the worst case is a waiting settlement" understated that bypass; it is
  replaced by the above.
- Tests: `test_a_direct_migrate_is_refused_while_another_process_holds_the_supervisor_lock` (a real
  second process holds the lock: a direct upgrade and a downgrade are refused, the version stays 2,
  a hosted database still migrates; after the holder exits the upgrade succeeds; the holder is
  stopped only by its own handle), `test_a_migration_is_refused_while_an_unstamped_unexpired_claim_exists`
  (refused at version 2; the same claim does not block a hosted database; once the lease expires
  the upgrade succeeds).

### Item 2 (P2) — settlement ownership

- `SettlementLedger._check_owner` (`src/idea_web/jobs/worker.py:430`), inside the settlement
  transaction: the job must exist (`:434`) and its `jobs.run_id` must equal the settlement's run
  (`:436`). A missing job or a null `run_id` is now a `LedgerConflict`.
- Legacy null-run adoption is a separate, explicit class, `LegacyRecoveryLedger`
  (`worker.py:573`), with its own validated association (`:581`): the job exists, is stopped and
  still has no run id; no job owns the run; and no settlement of the run names a different job.
- Only `_recover_unidentified` uses it (`local.py:586`, via `_settle_run(..., legacy=True)`,
  `local.py:499`); every other writer uses the exact owner check.
- Tests: `test_a_settlement_needs_an_existing_job_that_owns_the_run[missing|null]`,
  `test_legacy_null_run_adoption_is_an_explicit_validated_recovery`. The round-5 end-to-end
  pre-upgrade recovery test still passes through the new path.

### Item 3 (P2) — docstring

`SettlementLedger.settle` (`worker.py:388-390`) now says a pathless row belongs to a run whose media
cannot be located right now. It may hold paid spend, conservatively folded from SQLite alone, and
`attach` projects it once the media is found.

### Existing tests updated

None.

### Adversarial money risks — still awaiting the owner's decision

Unchanged and not widened.

### Residual risks (realistic)

- A job with no provenance stamp and missing media keeps waiting for its media (retried, never
  dropped).
- Hosted paid money stays deferred behind the round-6 refusal; hosted migrations take no lock.
- Queue `Worker(local_mode=True)` remains test-only.

### Gate outputs (final code, all in the foreground)

```text
round-8 tests before this pass: 5 failed
round-8 tests after:            5 passed
round 4-8 + local queue + worker suites after the patch: 84 passed, 1 warning in 76.50s

tests/idea_web:                 201 passed, 1 warning in 187.56s
top-level a-o and phase*:       4 failed, 526 passed, 1 warning in 345.48s
  -> exactly the four known environmental retention failures
shard B (the rest):             662 passed, 1 skipped, 93 deselected, 1 warning in 122.49s

Net: 1,389 passed, 1 skipped, and only the four known retention failures.
uv run ruff check .          -> All checks passed!
uv run ruff format --check . -> 311 files already formatted
uv run python scripts/audit_fixtures.py -> audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  -> page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data -> (empty)
leftover processes (psutil scan, command lines naming this worktree) -> none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Round-9 pass: atomic migration exclusion and a normalised local identity (after the round-8 sol xhigh review)

The round-8 review came back FIX_FIRST with two P0s:
1. The old-claim check ran in autocommit before `executescript` took the write lock, each script
   committed separately, and `schema_migrations` could be created before the supervisor lock.
2. `.IDEA/APP.DB` (Windows preserves casing) was classified as hosted, skipping the exclusion.

This pass changes only `src/idea_web/database.py` (plus the new test file).

Tests first: `tests/idea_web/test_followup_round9.py` (3 cases) against the round-8 code gave
3 failed:
- the racing claim hook never fired;
- 0002 committed on its own when 0003 failed;
- `.IDEA/APP.DB` was not local.

After the pass: 3 passed. Each fix was also reverted in turn (`scratchpad/revert_round9_demos.py`,
original bytes restored after each run):

```text
R1 claim check outside the write transaction (autocommit, then BEGIN): 1 failed
R2 each script commits on its own: 1 failed
R3 case-sensitive .idea/app.db names (round-8 check): 1 failed
R4 the \\?\ prefix is not stripped: 1 failed
R5 no supervisor lock for a local database: 1 failed
all reverted fixes fail their tests
```

Before writing it, a probe split all six migration scripts with `sqlite3.complete_statement`. Every
trigger body (0001's two append-only triggers, 0003's provenance trigger and its downgrade guard)
stayed whole, and every up script and every down script executed inside one open transaction. So
the atomic multi-script path works for every script, and the per-script fallback is not needed.

### Item 1 (P0) — the exclusion is one write transaction

`Database.migrate` (`database.py:213-268`), in this order:

1. With the cross-process migration file lock held, read the applied migrations without any DDL
   (`_applied`). If nothing is pending, return without writing (`:232`).
2. Take the supervisor lock for a local work root before any DDL (`_acquire_supervisor`, `:233`,
   `:302`), or raise `MigrationRefused`.
3. On the one connection (`isolation_level=None`), run `BEGIN IMMEDIATE` (`:234`).
4. Test seam `before_claim_check` (`:149`) fires here, inside the write transaction.
5. Inside that transaction, refuse any unexpired claim without an authority token
   (`_refuse_live_unproven_claims`, `:237`, `:320`; before 0003, any unexpired claim).
6. Create `schema_migrations` if missing, then re-read what is applied inside the transaction.
7. Apply every pending up or down script statement by statement with `connection.execute`
   (`_statements`, `:52`, split with `sqlite3.complete_statement` at `:59`; `:245`, `:254`), with
   no `executescript`, recording each `schema_migrations` row in the same transaction.
8. `COMMIT` once (`:259`). On any error, roll back. The connection is closed and the supervisor
   lock released in `finally` (`:267`).

A claim that races the check therefore cannot commit before it or around it: its writer meets
SQLite's write lock. A claim committed earlier is seen by the check and refuses the migration.

The single transaction applies to hosted databases too, the same code path, so they also commit
all pending scripts once. The lock and claim check apply only to a local work root.

Tests:
- `test_a_claim_racing_the_migration_check_cannot_commit_before_or_around_it`: the hook inserts a
  v2-style claim from a second connection with `busy_timeout = 0`. It gets exactly
  `database is locked`, the migration commits to 3, and no job row exists.
- `test_every_pending_script_commits_once_or_not_at_all`: 0003 is broken on a v1 database. The
  migration raises and the version stays 1, with no 0002 or 0003 columns. Unbroken, it reaches 3.

### Item 2 (P0) — one normalised local identity

- `_local_identity` (`database.py:31`) resolves the path, strips a `\\?\` or `\\?\UNC\`
  prefix (`:43`), applies `os.path.normcase` (`:44`), and compares the case-folded basename to
  `app.db` and its parent to `.idea` (`:45-46`).
- `is_local_work_root` (`:290`) and `supervisor_lock_path` (`:294`) both derive from that one
  identity, so every alias of one database contends on one supervisor lock.
- Test: `test_case_relative_and_extended_aliases_of_a_local_database_share_one_supervisor_lock`.
  A physically cased `.IDEA/APP.DB`, a relative `.IDEA/APP.DB` (cwd = work root) and a `\\?\`
  alias all classify as local and derive the same normalised lock path as
  `<root>/.idea/worker-supervisor.lock`. While a second process holds that lowercase lock, each
  alias's `migrate()` is refused with "stop the running ID'er first" and stays at version 2. After
  the holder exits (stopped only through its own handle), the migration reaches 3.

### Existing tests updated

None.

### Adversarial money risks — still awaiting the owner's decision

Unchanged and not widened.

### Residual risks (realistic)

- Unchanged from round 8: an unstamped job with missing media keeps waiting for its media; hosted
  paid money stays deferred behind the round-6 refusal; queue `Worker(local_mode=True)` remains
  test-only.
- A `migrate()` with nothing pending no longer creates an empty `schema_migrations` table
  (`version()` already treats a missing table as 0).

### Gate outputs (final code, all in the foreground)

```text
round-9 tests before this pass: 3 failed
round-9 tests after:            3 passed
rounds 2, 4-9 + worker + local queue after the patch: 100 passed, 1 warning in 157.19s

tests/idea_web:                 204 passed, 1 warning in 189.18s
top-level a-o and phase*:       4 failed, 526 passed, 1 warning in 364.85s
  -> exactly the four known environmental retention failures
shard B (the rest):             662 passed, 1 skipped, 93 deselected, 1 warning in 236.31s

Net: 1,392 passed, 1 skipped, and only the four known retention failures.
uv run ruff check .          -> All checks passed!
uv run ruff format --check . -> 312 files already formatted
uv run python scripts/audit_fixtures.py -> audited 460 files / fixture audit passed
uv run python scripts/check_page_js.py  -> page JavaScript check passed: 53 inline scripts across 22 page renders
git status --short -- work data -> (empty)
leftover processes (psutil scan, command lines naming this worktree) -> none
```

Offline only (`IDEA_TEST_MODE=1`, fake providers), no live provider call. `work/`, `data/`,
playlists, `README.md`, `idea.cmd` and `theme.py` are untouched, and nothing is committed.

## Owner decision on the two adversarial money risks (2026-09-15)

After round 9 the owner was asked about the two `[adversarial, money]` risks that every review since round 3
recorded as awaiting his decision:

1. a deliberately hostile program running as the owner that bypasses the application lock and races the
   deterministic attempt check, so one clip is dispatched twice;
2. a deliberately hostile program running as the owner that deletes or coherently rewrites both the JSONL
   attempt ledger and the SQLite money rows, so recorded spend disappears and paid work is resent.

**The owner accepted both** ("ok accept both"). They are accepted residual risks for the local tool: no
external or tamper-evident money ledger and no cross-process exclusive attempt creation will be built for
them. Hosted paid dispatch remains refused; if it is ever enabled, these risks must be re-assessed under a
multi-user threat model.

## Round-10 pass: tests only (after the round-9 sol xhigh review)

The round-9 review found no production defect. This pass adds tests only; no production file
changed (`src/idea_web/database.py` SHA-256 `d9629f64…9005aab` before and after). The owner-decision
section above is unchanged.

All changes are in `tests/idea_web/test_followup_round9.py`:

1. `test_case_relative_and_extended_aliases_of_a_local_database_share_one_supervisor_lock` is now
   Windows-only (`skipif sys.platform != "win32"`). The new cross-platform
   `test_canonical_and_relative_local_aliases_share_one_supervisor_lock` checks that the canonical
   `<root>/.idea/app.db` and a relative `.idea/app.db` (cwd = work root) are both local, share one
   normalised lock path, and are both refused while a second process holds that lock.
2. `test_the_in_transaction_reread_sees_a_migration_applied_after_the_preflight`: on a v1 database,
   another connection commits 0002 in full between `migrate()`'s preflight `_applied` read and its
   write transaction. `schema_migrations` then holds 1, 2 and 3 exactly once each and the 0002/0003
   columns exist: no migration applied twice, none skipped.
3. `test_a_refused_migration_of_a_fresh_local_database_creates_no_schema_object`: on a fresh local
   database, with the supervisor lock held by a second process, the refused migration leaves
   `sqlite_master` empty (no `schema_migrations`, no other object).
4. `test_a_unc_extended_alias_has_the_ordinary_unc_identity_and_lock` (Windows-only, pure identity:
   `Path.resolve` is the identity, so no share is touched): `\\\\?\\UNC\\server\\share\\work\\.IDEA\\APP.DB`
   normalises to `\\\\server\\share\\work\\.idea\\app.db`, the same identity as the ordinary UNC
   spelling, and both give the same `worker-supervisor.lock` path.

Revert proof (`scratchpad/revert_round10_demos.py`). Each guarded behaviour of `database.py` was
reverted in turn, its test run, and the file restored and checked byte-identical by SHA-256 after
every run:

```text
D1 no supervisor lock for a local database: 1 failed | DID NOT RAISE MigrationRefused | restored byte-identical=True
D2 migrate reuses the preflight snapshot: 1 failed | sqlite3.OperationalError: duplicate column name: attached | restored byte-identical=True
D3 schema_migrations created before the supervisor lock: 1 failed | assert [('table', 'schema_migrations')] == [] | restored byte-identical=True
D4 the \\\\?\\UNC\\ prefix is not normalised: 1 failed | 'unc\\server\\...' != '\\\\server\\...' | restored byte-identical=True
database.py sha256: d9629f647668890155aff3c9393789e0732147ba71a2029de54e79a789005aab
all reverted behaviours fail their tests
```

Results (foreground; only a test file changed and nothing shared, so the full suite was not re-run):

```text
tests/idea_web/test_followup_round9.py: 7 passed, 1 warning in 37.77s
tests/idea_web:                          208 passed, 1 warning in 266.95s
uv run ruff check .          -> All checks passed!
uv run ruff format --check . -> 312 files already formatted
git status --short -- work data -> (empty)
leftover processes -> none
```

Offline only, fake providers, nothing committed.

## Orchestrator gate runs on the final code

The worktree guard in the fixer's session refused PowerShell, so the orchestrator ran both PowerShell gates from the fixer's worktree against the final code, after checking the loopback ports were free:

```text
=== money fix applied to main (base 1c18454) — orchestrator gate runs ===
ruff check .            : All checks passed!
ruff format --check .   : 316 files already formatted
scripts/audit_fixtures.py: audited 464 files; fixture audit passed
scripts/check_page_js.py : page JavaScript check passed: 53 inline scripts across 22 page renders
pytest shard A (tests/idea_web, tests/test_[a-o]*.py, tests/test_phase*.py): 759 passed, 1 warning in 758.04s (0 failed; the four long-path retention cases pass on main)
pytest shard B (all remaining tests/test_*.py): 663 passed, 93 deselected (default slow/live markers), 1 warning in 167.10s (0 failed)
full suite on main: 1,422 passed, 0 failed
smoke_serve.ps1 (11:15:03-11:15:07, truth fixer pytest running elsewhere; ports free, cpu 9%): smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'; exit 0
gate_local_mode.ps1 (11:15:13-11:15:25): prepared offline cached mix; local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok; exit 0 (first run, no re-run needed)
```

Second-model reviews (Codex gpt-5.6-sol at xhigh, model verified from each log header), filed in round order:

- `docs/reviews/followup-money-sol-r1.md`
- `docs/reviews/followup-money-sol-r2.md`
- `docs/reviews/followup-money-sol-r3.md`
- `docs/reviews/followup-money-sol-r4.md`
- `docs/reviews/followup-money-sol-r5.md`
- `docs/reviews/followup-money-sol-r6.md`
- `docs/reviews/followup-money-sol-r7.md`
- `docs/reviews/followup-money-sol-r8.md`
- `docs/reviews/followup-money-sol-r9.md`

