# Build review — 4b-iv coalescing, subscribers, cancel/drain

Base: `48ffb63`. Not committed (per the brief). `src/id_detector/` diff: **0 lines**.

## In plain terms

When two people ask for the same mix, only one analysis now runs, and both people are recorded as
waiting on it, each with a held reservation. The person who started it pays. If that person
cancels while others are still waiting, the analysis keeps running. The bill moves to whoever
joined next, and the run's dollar reservation moves with it. If that person's monthly cap can't
cover it, the run still continues, the excess is charged to the global pool, and an audit row is
written. When the last person cancels, the run stops. Requests already sent to a provider are
settled as spent through the existing settlement path, and no new request can leave. Private
requests never share a run. A Free request can never join a paid run, and hosted paid analysis is
still refused.

## What changed, file by file

- `src/idea_web/migrations/0005_run_subscribers.up.sql` (new)
  - Adds `jobs.user_id`.
  - Adds `run_subscribers`: one row per job and run. It holds a reservation id, the recipe, the
    reserved size in whole minutes, a state (`held | released | settled`), and the attached,
    detached and released times. `seq` is the attach order.
    - The trigger `run_subscribers_attach_guard` refuses a subscriber on a finished run, one whose
      recipe differs from the run's, and a second subscriber on a non-public run.
    - A second trigger refuses deletes.
  - Adds `run_payer_events`, append-only (`attach | detach | transfer | release | close`).
    - `UNIQUE(run_id, kind, reservation_id)` plus a partial unique index on transfer targets make
      each change exactly-once at the database level.
    - A transfer row carries `usd_e6_reattributed` and `usd_e6_overage`; a close row carries the
      terminal status.
  - Adds `admin_audit` (plan §4.5 shape plus `detail`), append-only.
- `src/idea_web/migrations/0005_run_subscribers.down.sql` (new): refuses to run while any of the
  three tables holds a row (the same temp-trigger guard 0003 uses). Otherwise it drops them and
  `jobs.user_id`. It runs through `Database.migrate()`, which is unchanged.
- `src/idea_web/jobs/worker.py`
  - New: `QuotaExceeded`, the `UsdCapSeam` protocol (`account_month_remaining_e6`; 4d-iv will
    implement it), `reservation_minutes`, `_payer_event`, `_run_usd_reserved` (the larger of
    `analysis_runs.usd_e6_reserved` and `run_reservations`), `close_subscriptions` and
    `_mirror_attached`.
  - `Job.user_id`; `JobQueue.enqueue(user_id=)`; `JobQueue(usd_caps=)`; `Worker(usd_caps=)`.
  - `ReservationSeam`: an optional `reserve_for_subscriber(connection, job, intake, run_id)`. Either
    reservation call may raise `QuotaExceeded`. The seam runs inside a SAVEPOINT, and a refusal
    ends the job `quota_exceeded` with no run, no subscriber row, and none of the seam's writes.
  - `Worker._commit_intake`, all in the existing single intake transaction:
    - It refuses a hosted paid recipe (`HOSTED_PAID_REFUSAL`), even when a run for its key exists.
    - It attaches only if all of these hold: public scope on both sides; the same `analysis_key`
      and `requested_recipe_id`; an active run with a payer; a driving job with no cancel
      request; and at least one live held subscriber.
    - An attaching job gets a provisional subscriber row. A new run gets `payer_user` and
      `payer_reservation_id`, plus the initiator's subscriber row.
  - `JobQueue.request_cancel`: a job with a subscriber row now goes through `_detach`, in the same
    `BEGIN IMMEDIATE` transaction. Jobs without one (every local job, and pre-0005 rows) follow the
    old code path unchanged.
    - The detach is compare-and-set (a second detach changes nothing and returns `True`).
    - A non-payer's reservation is released.
    - The payer detaching while others remain triggers `_transfer_payer`:
      - a CAS on `payer_reservation_id` moves the payer to the earliest live subscriber;
      - the old reservation is released;
      - the run's USD reservation is re-attributed;
      - the cap is read inside the transaction, and any overage writes an `admin_audit` row;
      - the run is never stopped for any of this.
    - When the last subscriber detaches, `cancel_requested=1` is set on the run's driving job. That
      is the existing cancel token and the existing dispatch-admission refusal. The payer's
      reservation stays held, so a run with attempts in flight always has a payer.
    - An attached job that detaches is ended `cancelled` at once, as before.
  - `JobQueue.terminal`, in the same transaction that makes the run terminal:
    - the payer's reservation becomes `settled` (a `close` event); every other held reservation is
      released;
    - attached waiting jobs get the run's status and bundle (so a draining worker's subscribers
      no longer wait for a reconciliation pass);
    - a driving job whose own user detached is recorded as `cancelled`.
  - `_abandon_run` (quarantine or dead letter) closes subscriptions the same way.
  - Nothing new computes or settles money. Settlement stays `run_settlements` plus the folded
    `analysis_runs` row; the `close` event records only whose reservation that settlement is
    attributed to.
- Tests adjusted to the new schema:
  - `test_worker.py`, `test_followup_round2/6/7/8/9.py`: the schema version 4 → 5.
  - `test_ops.py::test_an_attached_job_carries_the_page_document`: its driver now goes through real
    intake. A bare run row has no payer or subscriber, and such runs are deliberately not
    attachable.

Protected and other-session files are untouched: `docs/PLAN-v2.md`, `profiles/`, `data/`, `work/`,
`src/id_detector/**`, `README.md`, `idea.cmd`, `tests/test_playlists.py`, and `theme.py`.

## Tests added — `tests/idea_web/test_coalescing.py` (25)

**Gate item 1 — one run, two reserved subscribers**
- `test_two_users_with_the_same_key_get_one_run_and_two_reserved_subscribers` (second worker on its
  own handle; seam called for both inside the transaction; payer = initiator; release and close at
  the terminal status; attached job mirrored in the same transaction)
- `test_two_intakes_racing_for_one_new_key_make_exactly_one_run` (two threads, barrier)
- `test_a_refused_reservation_is_quota_exceeded_and_never_attached` (seam writes rolled back; new
  run refused too)

**Gate item 2 — initiator detaches, payer transfers**
- `test_the_initiator_detaching_moves_the_payer_to_the_earliest_subscriber_with_its_usd`
  - A real local-mode paid run with a reservation row and admission-fenced dispatches.
  - The payer goes to the earliest attacher (`zoe` before `adam`), who inherits the full USD
    reservation; overage = reserved − cap, and the audit row is checked exactly.
  - After the transfer the cancel token has not fired and a new paid request is admitted.
  - There is exactly one settlement (2 units), with 2 dispatch rows.
- `test_a_transfer_within_the_new_payers_cap_is_not_audited`
- `test_a_hosted_free_run_keeps_running_for_its_subscriber_after_the_initiator_detaches` (zero USD,
  so the cap is not consulted)
- `test_a_reclaimed_driver_whose_user_detached_still_finishes_for_the_subscribers`

**Gate item 3 — last detach cancels and settles**
- `test_the_last_detach_cancels_the_run_and_settles_its_in_flight_attempts`
  - Two requests are on the wire; both users detach at once (threads, barrier, separate handles).
  - The token fires, and a third dispatch is refused (`DispatchRefused`, no row).
  - The in-flight request resolves; the unanswered one is ambiguous. Both are spent, in one
    `run_settlements` row (status `cancelled`).
  - The payer is never NULL; the payer's reservation is `settled` and the rest are released.
- `test_a_non_payer_detaching_releases_only_its_own_reservation`
- `test_a_run_whose_last_subscriber_left_takes_no_new_subscribers`

**Races — exactly-once transfer**
- `test_two_concurrent_detaches_of_the_initiator_transfer_the_payer_once` (×4)
- `test_the_payer_and_its_successor_detaching_together_never_orphan_the_run` (×4, replays the event
  log: no transfer from a non-payer or to a detached subscriber)
- `test_two_processes_detaching_the_initiator_transfer_the_payer_once` (spawn, barrier)
- `test_the_event_log_itself_refuses_a_second_transfer_from_one_reservation` (plus the append-only
  and no-delete triggers)

**Gate item 4 — private scope, tiers, hosted refusal**
- `test_private_scope_never_coalesces` (same user twice gives two runs; a public request gets its
  own run; the trigger refuses a forged second subscriber)
- `test_a_free_request_never_attaches_to_a_paid_run_even_under_the_same_key` (plus the trigger)
- `test_coalescing_never_admits_a_hosted_paid_request` (the intake refusal, then the worker's
  dead letter; the paid run is untouched)

**Drain and migration**
- `test_a_draining_worker_finishes_its_run_and_its_subscribers_get_the_result`
- `test_migration_0005_goes_down_only_while_its_tables_are_empty`

**Reversion demonstration.** Each fix was reverted in turn by a script, the gate was run, and the
file was restored. Every reversion failed the gate:

```text
R1 private scope may coalesce: rc=1 | 1 failed, 24 passed | ['test_private_scope_never_coalesces']
R2 transfer may pick a detached subscriber: rc=1 | 14 failed, 11 passed | [8 tests incl. both race tests, the process race, gate-2 and gate-3]
R3 initiator detach fires the cancel token: rc=1 | 13 failed, 12 passed | [7 tests incl. gate-2, hosted-free, reclaimed-driver, races]
R4 attach ignores the recipe: rc=1 | 1 failed, 24 passed | ['test_a_free_request_never_attaches_to_a_paid_run_even_under_the_same_key']
R5 a second detach is not idempotent: rc=1 | 5 failed, 20 passed | ['test_two_concurrent_detaches_of_the_initiator_transfer_the_payer_once', 'test_two_processes_detaching_the_initiator_transfer_the_payer_once']
R6 no hosted paid guard at attach: rc=1 | 1 failed, 24 passed | ['test_coalescing_never_admits_a_hosted_paid_request']
R7 terminal does not close subscriptions or mirror: rc=1 | 7 failed, 18 passed | [gate-1, intake race, drain, gate-2, gate-3, hosted-free, reclaimed-driver]
R8 a refused seam's writes are kept: rc=1 | 1 failed, 24 passed | ['test_a_refused_reservation_is_quota_exceeded_and_never_attached']
R9 overage not audited: rc=1 | 1 failed, 24 passed | ['test_the_initiator_detaching_moves_the_payer_to_the_earliest_subscriber_with_its_usd']
R10 attach to a run whose last subscriber left: rc=1 | 1 failed, 24 passed | ['test_a_run_whose_last_subscriber_left_takes_no_new_subscribers']
R11 detached driver keeps the run's status: rc=1 | 3 failed, 22 passed | [hosted-free, reclaimed-driver, gate-2]
```

## How users and subscribers are modelled

- **User.** An opaque text id supplied at submission (`JobQueue.enqueue(user_id=...)`), stored in
  `jobs.user_id`, copied to `run_subscribers.user_id` and `analysis_runs.payer_user`. It is NULL
  for local or anonymous requests. There is no `users` table or foreign key; 4c-i adds both.
- **Subscriber.** One `run_subscribers` row per job that reached a run: the initiator when the run
  is created, and each attacher when it attaches.
- **Reservation.** The row's `reservation_id`, `minutes_reserved` (the mix length rounded up to
  whole minutes, the same for every subscriber) and `reservation_state`.
  - There are no credit lots. 4d-i plugs in through `ReservationSeam.reserve_for_new_run` and
    `reserve_for_subscriber`, both called inside the intake transaction with savepoint rollback on
    `QuotaExceeded`.
  - The settling reservation is the one named by `analysis_runs.payer_reservation_id`; every other
    held reservation is provisional.
- **USD.** No new USD table. The amount re-attributed is the run's existing reservation (0003's
  `run_reservations` or `analysis_runs.usd_e6_reserved`), recorded on the transfer event. The
  account-month cap is the `UsdCapSeam` (4d-iv); without one, nothing is capped.

## Plan ambiguities resolved

1. **Driver versus subscriber.** §3.5 says a run whose initiator detaches "continues (never
   aborted in flight)". So the initiator's job keeps driving (its claim, lease and fence are
   unchanged); its user's detach ends only their subscription. That job is recorded as
   `cancelled` when the run ends. §3.5's "cancel token" is the existing `cancel_requested` flag,
   set only when the last subscriber detaches.
2. **Amount re-attributed.** The whole run reservation (the initiator's reservations are released
   in full) is re-attributed, and the overage is `reserved − max(0, remaining)`. "Charged against
   the global pool" is recorded (`charged_to: global_pool`) but not debited: the global-day pool is
   4d-iv.
3. **`usd_events` / `usd_reservations` were not created.** They belong to 4d-i/4d-iv. Payer
   changes use `run_payer_events` instead, which never holds a money figure of its own beyond the
   re-attributed amount. `admin_audit` was created now because §3.5's overage path is its first
   writer.
4. **Tier.** Free versus paid is the recipe (entitlements are 4c/4d). The recipe is enforced by the
   attach query and by the insert trigger.
5. **Runs from before 0005** (no payer or subscriber) are not attachable. A new submission starts
   its own run, and cancelling such a run's jobs keeps the old behaviour. In practice none exist:
   hosted is not deployed and the local worker never creates `analysis_runs` rows.
6. **Drain.** `drain()` already stopped new claims. Since draining workers skip reconciliation,
   attached jobs now receive the result in the terminal transaction.
7. **Paid coalescing** is exercised with `Worker(local_mode=True)` (the only place paid dispatch is
   allowed); hosted paid stays refused at both the claim and the intake transaction.

## Decisions

- **Size (review P1, decided by the coordinator: do not split).** The cycle exceeds the plan's
  guideline of about 1,500 changed lines. After the fix pass it is about 2,265 lines: `worker.py`
  +655/−62, migration 0005 157 lines, `test_coalescing.py` 1,391 lines, and a few one-line test
  edits. Most of that is tests. It stays whole because splitting a payer-transfer change would
  leave an intermediate state where money changes hands without its fence and its tests.

## Could not do / left open

- A detached driver's page document still describes the run's own outcome (its job `state` is
  `cancelled`). Hosted job pages are 4d-iii; not changed here.
- `alias_id` stays NULL until `source_aliases` (6a-i).
- `docs/STATUS.md` was not updated (not asked for in the brief).
- Not run: the PowerShell gates, and any server on 8791/8792 (per the brief).

## Pasted outputs

### 1. Full suite, in foreground shards (every collected file)

`uv run pytest --collect-only -q` → `1828/1925 tests collected (97 deselected) in 13.39s` (96 files:
17 under `tests/idea_web`, 79 under `tests/`).

```text
A  idea_web: test_backup, test_coalescing, test_followup_queue_money, test_followup_review_fixes,
   test_followup_round2/4/5/6/7/8/9                                    166 passed in 276.29s
B  idea_web: test_headers_forms, test_legacy_contract, test_local_queue, test_ops, test_parity,
   test_worker                                                          161 passed in 86.37s
C  acrcloud_clip … paid_clip (14 files)                                 154 passed in 104.98s
D1 phase0a_crash_cache, phase0a_money, phase0a_security, phase0a_status, phase0b_attempts
                                                                         94 passed in 144.93s
D2 phase0b_audd, phase0b_config, phase1a_bundles, phase1a_cached_open, phase1a_compat
                                                                        151 passed in 237.65s
D3 phase1b_breaker_scorer, phase1b_fusion, phase1b_targeting, phase2b_retention
                                                                        148 passed in 221.71s
D4 phase3a_honesty, playlists, projection, scan, scan_targeting, score_corpus, semantics
                                                                        162 passed in 99.65s
E1 service_api, stage10_webapp, stage1_jobs/privacy/process/shazam/wheel/windows
                                                                         92 passed in 109.80s
E2 stage2a_*, stage2b_*, stage3_*, stage4a_* (11 files)                 230 passed in 81.28s
E3 stage4c_*, stage4d, stage5, stage6, stage7_*, stage8_*, stage9_* (13 files)
                                                     165 passed, 1 skipped, 1 deselected in 31.90s
F  truth_corpus_followup* (10 files), truth_gateway_guard, truth_review
                                                     303 passed, 1 skipped, 4 deselected in 240.91s
```

Total: 1826 passed + 2 skipped = **1828**, equal to the collected count; 0 failed. (Shard B first
failed on `test_ops.py::test_an_attached_job_carries_the_page_document`, which was fixed as
described above and re-run.) A first attempt at a larger shard D hit the 590 s shell cap and was
killed before finishing; no process was left behind, and the shard was re-run in the four pieces
above.

### 2. Ruff

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
375 files already formatted
```

### 3. Scripts

```text
$ uv run python scripts/audit_fixtures.py
audited 513 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

### 4. The gate

```text
$ uv run pytest tests/idea_web/test_coalescing.py -q
25 passed, 1 warning in 17.44s
```

### 5. Git

```text
$ git diff --stat
 src/idea_web/jobs/worker.py            | 426 +++++++++++++++++++++++++++++++--
 tests/idea_web/test_followup_round2.py |   2 +-
 tests/idea_web/test_followup_round6.py |   4 +-
 tests/idea_web/test_followup_round7.py |   2 +-
 tests/idea_web/test_followup_round8.py |   8 +-
 tests/idea_web/test_followup_round9.py |  12 +-
 tests/idea_web/test_ops.py             |   8 +-
 tests/idea_web/test_worker.py          |   6 +-
 8 files changed, 427 insertions(+), 41 deletions(-)
$ git status --short
 M src/idea_web/jobs/worker.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_ops.py
 M tests/idea_web/test_worker.py
?? src/idea_web/migrations/0005_run_subscribers.down.sql
?? src/idea_web/migrations/0005_run_subscribers.up.sql
?? tests/idea_web/test_coalescing.py
$ git diff -- src/id_detector | wc -l
0
```

(`docs/reviews/build-4b-iv.md`, this file, is also new and untracked.)

The outputs above are from the first build. The fix pass's outputs are below and supersede them.

---

## Fix pass (review `diff-review-4b-iv-sol`, verdict FIX_FIRST)

### What was fixed

| Review item | Reproduced by | Fix |
|---|---|---|
| **P0-1** A payer transfer before the reservation existed recorded $0, and the later reservation was never attributed or checked against a cap | `test_a_reservation_made_after_the_transfer_is_attributed_to_the_new_payer_and_cap_checked` | See note 1 below the table. |
| **P0-2** A last detach committed after the pipeline settled `complete` still left the run `complete` | `test_a_last_detach_after_the_pipeline_settled_complete_still_ends_the_run_cancelled` | See note 2. |
| **P0-3** A driver killed after the last detach: the replacement cancelled the run with no `run_settlements` row | `test_a_driver_killed_after_the_last_detach_is_settled_once_by_its_replacement` | See note 3. |
| **P1-4** A dead letter during drain left subscribers `waiting` | `test_a_dead_letter_during_drain_gives_its_subscribers_the_result` | See note 4. |
| **P1-5** No real private race | `test_two_private_submissions_racing_for_one_key_never_coalesce` | Two barrier-started private intakes for one key, both driving their own run at once. No code change was needed; the test is new. |
| **P1-6** Size | — | Recorded under "Decisions" (not split, per the coordinator). |

1. **P0-1.**
   - New `attribute_reservation()` runs inside `DispatchAdmission.reserve()`'s transaction. That is
     the one fenced insert of `run_reservations`.
   - It writes a `reserve` event (a new event kind, added to the still-unshipped migration 0005)
     attributing the new reservation to the payer at that moment.
   - If the payer is no longer the initiator, it checks that payer's month cap then and audits any
     overage (`trigger: reservation_after_transfer`).
   - `_run_usd_reserved` now reads only `run_reservations`. A transfer therefore moves either the
     whole existing reservation or nothing, and nothing is counted twice.
   - `usd_caps` is passed from `Worker` through `SQLiteAttemptJournal` to `DispatchAdmission`.
   - Transfer and reservation overages share `_cap_overage` / `_audit_overage`; the audit detail
     now names its `trigger`.
2. **P0-2.**
   - `JobQueue.terminal()` now reads the job's `cancel_requested` flag and the run's live
     subscribers in its own transaction. If the run had subscribers, none remains, and the token
     has fired, the result becomes `cancelled` (reason `every subscriber detached`, no `achieved`,
     no bundle registered).
   - `_settle_as_cancelled()` rewrites the existing `run_settlements` row's status and exit code
     to `cancelled` in the same transaction, without touching its money: dispatched spend stays
     spent. The journal line is reprojected after commit.
   - The payer's `close` event carries `cancelled`, which means 0 % credits under §3.5.
3. **P0-3.**
   - `Worker._cancel_before_start` now calls `_settle_if_absent()` before `terminal()`. It folds
     every durable attempt event, the authoritative `run_dispatches` rows (an unresolved dispatch
     counts as spent) and the `run_reservations` row.
   - It then writes through the existing `SettlementLedger(only_if_missing=True)` under the run
     fence. There is no second settlement authority.
4. **P1-4.**
   - One shared `mirror_attached()` helper is used by the terminal transaction, `_abandon_run()`
     (now called after it closes subscriptions) and `reconcile_attached()`, which is now only a
     per-run backstop calling that helper.

The `JobQueue.terminal()` change applies only to runs that have subscriber rows. Local jobs never
do, so local mode is unchanged. Existing tests were adjusted only for the new `reserve` event and
audit `trigger` in the gate-2 test, and for the within-cap test, which now writes the one
`run_reservations` row instead of the `analysis_runs` column.

### Reversion demonstration (fix pass)

Each fix was reverted in turn, the gate was run, and the file was restored byte-for-byte:

```text
F1 reservation not attributed to the current payer: rc=1 | 2 failed, 28 passed | ['test_a_reservation_made_after_the_transfer_is_attributed_to_the_new_payer_and_cap_checked', 'test_the_initiator_detaching_moves_the_payer_to_the_earliest_subscriber_with_its_usd']
F2a terminal ignores a committed last detach: rc=1 | 1 failed, 29 passed | ['test_a_last_detach_after_the_pipeline_settled_complete_still_ends_the_run_cancelled']
F2b settlement row keeps the pipeline's status: rc=1 | 1 failed, 29 passed | ['test_a_last_detach_after_the_pipeline_settled_complete_still_ends_the_run_cancelled']
F3 cancel-before-start inserts no settlement: rc=1 | 1 failed, 29 passed | ['test_a_driver_killed_after_the_last_detach_is_settled_once_by_its_replacement']
F4 abandon does not mirror attached jobs: rc=1 | 1 failed, 29 passed | ['test_a_dead_letter_during_drain_gives_its_subscribers_the_result']
F5 private scope may coalesce (R1): rc=1 | 2 failed, 28 passed | ['test_private_scope_never_coalesces', 'test_two_private_submissions_racing_for_one_key_never_coalesce']
```

The P0-3 regression uses a real crash. A spawned child worker dispatches one paid request and
blocks. The parent attaches a subscriber and commits both detaches, then kills the child. A
replacement worker, whose clock is a minute ahead, reclaims the job. The test proves exactly one
settlement row, `cancelled`, with the one dispatched request counted as spent.

### Fix-pass outputs

`uv run pytest --collect-only -q` → `1833/1930 tests collected (97 deselected)`, the same 96 files
in the same 11 foreground shards:

```text
A 171 passed (335.02s)   B 161 passed (100.90s)   C 154 passed (117.01s)
D1 94 passed (197.55s)   D2 151 passed (267.89s)  D3 148 passed (200.24s)
D4 162 passed (135.37s)  E1 92 passed (127.68s)   E2 230 passed (52.54s)
E3 165 passed, 1 skipped, 1 deselected (21.36s)
F 303 passed, 1 skipped, 4 deselected (276.78s)
```

Total: 1831 passed + 2 skipped = **1833**, the collected count; 0 failed.

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
376 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 514 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
$ uv run pytest tests/idea_web/test_coalescing.py -q
30 passed, 1 warning in 21.25s
$ git diff --stat
 src/idea_web/jobs/worker.py            | 675 ++++++++++++++++++++++++++++++---
 tests/idea_web/test_followup_round2.py |   2 +-
 tests/idea_web/test_followup_round6.py |   4 +-
 tests/idea_web/test_followup_round7.py |   2 +-
 tests/idea_web/test_followup_round8.py |   8 +-
 tests/idea_web/test_followup_round9.py |  12 +-
 tests/idea_web/test_ops.py             |   8 +-
 tests/idea_web/test_worker.py          |   6 +-
 8 files changed, 655 insertions(+), 62 deletions(-)
$ git status --short
 M src/idea_web/jobs/worker.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_ops.py
 M tests/idea_web/test_worker.py
?? docs/reviews/build-4b-iv.md
?? src/idea_web/migrations/0005_run_subscribers.down.sql
?? src/idea_web/migrations/0005_run_subscribers.up.sql
?? tests/idea_web/test_coalescing.py
$ git diff -- src/id_detector | wc -l
0
```

Still open: the P2 local `/` smoke on a writable machine. It was not run because the brief forbids
servers on 8791/8792. Nothing was committed.

---

## Fix pass 2 (review `diff-review-4b-iv-sol-r2`: item 2 PARTIAL; items 1, 3, 4 and 5 DONE and untouched)

Code changes are in `worker.py` only. Test changes are in `test_coalescing.py` only.

- **P1: rewrite the whole settlement entry.** When the last detach wins over a success,
  `_settle_as_cancelled()` now rewrites the entry to the truth of a cancelled run:
  - `status=cancelled`, `exit_code=130`, and `reason` set to the detach reason;
  - `achieved`, `bundle_id`, `fuse_run` and `compatibility` all cleared (`None`);
  - the money is untouched (`analysis_key` also stays).

  The override already left the bundle unregistered. In addition, `_compatible_bundle` now serves
  only bundles whose run's own status is `complete` or `degraded`. A cancelled, failed or waiting
  run is never a cache hit, whatever its bundle manifest says.
  - Regression: `test_a_cancelled_run_keeps_no_trace_of_the_success_it_overrode`.
    - It is pipeline-shaped: a real published Deep bundle and a settlement carrying `achieved`,
      `bundle_id`, `fuse_run`, `analysis_key` and compatibility, then the last detach, then
      `terminal()`.
    - It asserts the whole entry and the journal line are cancelled, the spend is kept, and no
      bundle row exists.
    - A forged bundle row for the run is not served, and a new request starts a new run.
    - Control: the same bundle *is* served once the run's status is `complete`.
- **P1: make reprojection recoverable.**
  - `Worker.reproject_cancelled_settlements()` runs at the start of every `run_once`, so the first
    pass is the worker's start. It re-checks each `run_settlements` row that is cancelled with the
    all-subscribers-detached reason.
  - It uses the existing `SettlementLedger.reproject()` (reproject from the row), once per process
    per run. A failed repair is retried on the next pass. There is no second authority.
  - Regression: `test_a_failed_reprojection_after_the_cancel_is_repaired_on_the_next_start`.
    - An injected failure breaks the first cancelled projection, so the journal still says
      `complete`/`deep`.
    - A restarted worker's `run_once()` has nothing to claim, yet it rewrites the line so that it
      equals the row (`cancelled`, no achievement or bundle).
- **P2: seam removed.** `JobQueue.before_detach_commit` was unused, so it is gone.

**Reversions** (each fix reverted, the gate run, the file restored byte-for-byte):

```text
G1a settlement rewrite keeps the success's outcome fields: rc=1 | 2 failed, 30 passed | ['test_a_cancelled_run_keeps_no_trace_of_the_success_it_overrode', 'test_a_failed_reprojection_after_the_cancel_is_repaired_on_the_next_start']
G1b compatibility lookup serves any run's bundle: rc=1 | 1 failed, 31 passed | ['test_a_cancelled_run_keeps_no_trace_of_the_success_it_overrode']
G2 no reprojection repair on worker start: rc=1 | 1 failed, 31 passed | ['test_a_failed_reprojection_after_the_cancel_is_repaired_on_the_next_start']
```

**Outputs** (foreground). No file outside `worker.py` and the coalescing tests changed in this
pass, so the full suite was not re-run.

```text
$ uv run pytest tests/idea_web/test_coalescing.py -q
32 passed, 1 warning in 24.67s
$ uv run pytest tests/idea_web --collect-only -q
334 tests collected
shard A (test_backup, test_coalescing, test_followup_*)          173 passed in 351.91s
shard B (test_headers_forms, legacy_contract, local_queue, ops, parity, worker)
                                                                  161 passed in 108.09s
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
376 files already formatted
$ git diff -- src/id_detector | wc -l
0
$ git diff --stat | tail -1
 8 files changed, 702 insertions(+), 62 deletions(-)
```

The `tests/idea_web` total is 173 + 161 = 334, the collected count; 0 failed. Nothing was
committed.
