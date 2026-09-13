# Build review — 4b-i durable queue, intake, worker, checkpoints

## Changes

- `src/idea_web/database.py` — added SQLite connection policy (`WAL`, foreign keys, busy timeout),
  process-local writer serialisation, `BEGIN IMMEDIATE` write transactions, and the numbered
  up/down migrator.
- `src/idea_web/migrations/__init__.py` — made the numbered SQL migrations package data.
- `src/idea_web/migrations/0001_durable_queue.up.sql` — added the five 4b-i-owned tables,
  lookup indexes, constraints, and append-only attempt-event triggers.
- `src/idea_web/migrations/0001_durable_queue.down.sql` — removes both append-only triggers
  and drops `provider_attempt_events`, `jobs`, `result_bundles`, `analysis_runs`, then `media` in
  dependency order.
- `src/idea_web/jobs/__init__.py` — exported the framework-neutral queue and worker API.
- `src/idea_web/jobs/worker.py` — added untrusted target encoding/decoding, the durable job queue,
  leases and heartbeats, intake resolution and decision transaction, SQLite checkpoints, attempt
  event projection, cancellation, drain, retry/dead-letter handling, and the service worker.
- `tests/idea_web/__init__.py` — added the web-component test package without adding an app.
- `tests/idea_web/test_worker.py` — added the complete deterministic phase gate, including a real
  spawned worker termination and replacement.

No existing local-mode file changed. `idea serve`, `idea.cmd`, and `idea analyse` still use the
existing `id_detector.webapp.jobs` in-memory manager and existing runner. Nothing imports or adds
FastAPI, Starlette, uvicorn, or another HTTP framework, and no route, app, template, or page was
built.

## Schema and migrations

Migration `0001_durable_queue` creates only:

- `media`
- `analysis_runs`
- `result_bundles`
- `jobs`
- `provider_attempt_events`

The migration records enough compatibility input to call the existing `compat.serves()` rather
than reproducing its rules. `provider_attempt_events` has unique `(attempt_id, seq)` events and
database triggers that reject every update and delete. The down migration is exercised by a real
up-to-latest then down-to-zero test; only the migrator's `schema_migrations` ledger remains.

Deferred to their owning cycles: `users`, `sessions`, `email_tokens`, `credit_grants`,
`credit_allocations`, `credit_events`, `usd_reservations`, `usd_events`, `cache_hits`,
`library_items`, `run_subscribers`, `source_aliases`, `entitlements`, and `admin_audit`. There is a
named `ReservationSeam.reserve_for_new_run(...)`, invoked inside the new-run intake transaction,
for 4d-i to implement. This cycle contains no credit or hosted USD-reservation implementation and
does not fake either one.

## Job state machine, leases, cancellation and drain

Jobs enter as `intake`. A claim is one `BEGIN IMMEDIATE` transaction: the oldest eligible row with
no lease or an expired lease is fenced to `lease_owner`, assigned `lease_until`, receives its
heartbeat timestamp, and increments `attempt`. Intake resolves and durably takes/fetches the media,
decodes it, calculates duration and the hints snapshot, then derives the key through
`compat.AnalysisInputs.analysis_key`. One deciding `BEGIN IMMEDIATE` transaction then:

1. serves a bundle for which the existing `serves()` returns true;
2. changes the job to `waiting` on an existing non-terminal run; or
3. inserts `media` and `analysis_runs` and changes the job to `analysis`.

An attached `waiting` job is never claimable for pipeline execution; reconciliation mirrors the
one underlying run's terminal state. A provider-breaker `waiting` retry is atomically changed back
to `analysis` before calling `id_detector.service.run`. Service outcomes atomically update the run,
insert a verified durable bundle row when present, and move the fenced job to its terminal status.

The default heartbeat interval is exactly 10 seconds and the default lease is 30 seconds. A
heartbeat renews only a non-terminal row still owned by that worker. An expired lease is reclaimable
with the attempt incremented, while every progress/final transition checks `lease_owner`; the old
worker cannot publish queue state after losing its fence. A failure releases the lease for retry.
The third failed attempt (`attempt == max_attempts == 3`) becomes `dead_letter` with its reason;
`expire_dead_letters()` changes it to externally terminal `failed` at 24 hours.

`cancel_requested` is checked before work, by the token before provider dispatch, and on every
progress update. Mid-phase cancellation unwinds through the service's cancel path after in-flight
attempts settle; the job becomes `cancelled` while all earlier durable checkpoints remain available
for a future run. `drain()` prevents new claims and lets the current leased job finish; the worker
loop also performs the 24-hour dead-letter expiry pass.

Queue target JSON is treated as untrusted on every read. It must decode to exactly
`PlatformUrl | UploadId | LocalPath`; platform and upload values call the existing
`validate_platform_url` / `validate_upload_id`. `LocalPath` is refused both when enqueued and when a
tampered row is read unless the worker is explicitly in local mode.

## Checkpoints and provider attempts

`SQLiteCheckpointStore` implements the 4a-i protocol, including `state()`, in
`analysis_runs.checkpoints`. It proves every referenced artefact exists before committing the
checkpoint. The intake's already-produced ingest/decode artefacts are checked the same way before
their initial checkpoint document is inserted. A failure injected after artefact publication but
before the checkpoint transaction leaves the phase incomplete, and the retry performs it again.

The real-process gate terminates the first worker only after the `primary` checkpoint transaction
has committed. Once its short test lease expires, a replacement claims the same run. The next
checkpoint is `hints`, the replacement fake AudD adapter receives zero calls, and the original 21
prepared/dispatched/resolved events and original reservation/spend remain on the run. Known clips
are therefore not re-sent or re-charged through the queue recovery path.

`SQLiteAttemptJournal` retains the service's crash-recovery JSONL contract and appends the hosted
projection as attempts resolve. Rows contain `attempt_id`, `seq`, `run_id`, `provider`, `egress_id`,
`query_id`, `parent_attempt_id`, `state`, `outcome`, derived `http_status`, `unit_usd_e6`, and `at`.
No breaker policy was added; 4b-ii can read this append-only table.

## Tests added

`tests/idea_web/test_worker.py` covers:

- migrations up/down, WAL, foreign keys, and exactly the five owned tables;
- observable `intake -> analysis` before service execution;
- third-attempt dead letter and the 24-hour failed transition;
- expired-lease reclaim and old-owner fencing;
- mid-phase cancel-token propagation with durable resumable checkpoints;
- artefact/checkpoint crash ordering and redo on retry;
- compatible intake serving without a service/pipeline call;
- hosted `LocalPath` refusal on both trusted write and tampered database read;
- all three append-only provider attempt events and trigger-enforced update/delete refusal;
- the 10-second heartbeat/no-framework boundary and drain behavior; and
- real child-process death after `primary`, lease expiry, and zero-AudD recovery through the queue.

All tests are offline and deterministic. The real-process test uses `FakeAudD`, `FakeShazamHTTP`,
and `no_backoff`; it asserts adapter call counts and database state, never log text. No dependency
was added.

## Gate outputs

`uv run pytest -q`

```text
........................................................................ [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1152 passed, 93 deselected, 1 warning in 597.79s (0:09:57)
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
282 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 452 files
fixture audit passed
```

`uv run pytest tests/idea_web/test_worker.py -q`

```text
.............                                                            [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
13 passed, 1 warning in 65.15s (0:01:05)
```

`uv run pytest tests/test_service_api.py tests/test_phase0a_money.py tests/test_phase0a_status.py tests/test_phase0b_attempts.py tests/test_phase1a_compat.py tests/test_phase1b_breaker_scorer.py tests/test_phase2b_retention.py tests/test_playlists.py tests/test_golden_local_free.py tests/test_stage10_webapp.py -q`

```text
........................................................................ [ 28%]
........................................................................ [ 56%]
........................................................................ [ 84%]
.......................................                                  [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
255 passed, 1 warning in 229.17s (0:03:49)
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-38e98918a475418ab2db9594dae7a01c\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\57ff1f8f841003aebdf1b534476bba820f98d344775e00bcdda930910ea63aef\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-38e98918a475418ab2db9594dae7a01c
```

`git status --short`

```text
?? docs/reviews/build-4b-i.md
?? src/idea_web/database.py
?? src/idea_web/jobs/
?? src/idea_web/migrations/
?? tests/idea_web/
```

`git diff --stat`

produced no output because every cycle deliverable is a new, untracked file.

`git status --short -- work data`

produced no output.

The protected-file status check for `src/id_detector/playlists`, `tests/test_playlists.py`,
`README.md`, `idea.cmd`, and `src/id_detector/present/theme.py` was also empty. Nothing was
committed, branched, pushed, or run against a live provider. Nothing in scope could not be done.

BUILD: COMPLETE

## Review + fix pass (sol xhigh review folded in)

Adversarial review of the uncommitted 4b-i tree against §4.6, §4.5, §4.2's rules line and §4.3,
with Codex sol's read-only findings re-verified line by line. Every finding below was reproduced
before it was fixed, and each fix has a test that fails without it. Line numbers are the ones in
the tree as sol reviewed it.

### Findings, verdicts and fixes

**P0-1 — checkpoints, money and journal writes were not fenced. Verdict: correct.**
`worker.py:211` updated `analysis_runs.checkpoints` (and, for `primary`, reservation, spend and
attempts) keyed only on `run_id`; `worker.py:270` inserted attempt events with no ownership check;
the only fence anywhere was `lease_owner = worker_id`, which a restarted worker reuses.
*Fix:* `jobs.claim_token` and `analysis_runs.claim_token` — a claim **generation** (uuid4), never a
worker identity. `claim()` mints one per claim inside its transaction and rotates it onto the run.
Checkpoint writes, primary-money writes, both attempt projections, progress, heartbeat,
`begin_analysis`, `wait`, `fail` and `terminal` all carry `AND claim_token = ?` and raise
`StaleClaim` when refused; `terminal` also clears the run's token, so a settled run accepts nothing
further. *Test:* `test_a_reclaimed_claim_cannot_write_checkpoints_money_or_attempts` reclaims the
job **during** execution and proves the old claim can no longer checkpoint, settle, journal (and
therefore cannot dispatch) or publish progress, while the replacement's money and recovery state
stand.

**P0-2 — file existence was mistaken for durability. Verdict: correct.**
`worker.py:206` and `:923` checked only `path_is_file`, and the PCM the intake checkpoint names is
published by `decode.py:139` with `os.replace` of an ffmpeg temporary that nobody fsyncs.
*Fix:* `fsync_artefact` / `require_durable` flush every named artefact — and, on POSIX, its
directory entry — before the checkpoint row commits, on both the service and the intake path.
*Tests:* `test_a_checkpoint_never_commits_ahead_of_its_artefact` asserts the flush happens before
the commit and that a checkpoint whose artefact is missing or unflushable does not commit;
`test_intake_checkpoints_flush_their_artefacts_before_the_intake_transaction` covers intake.

**P0-3 — cancellation erased recovered money. Verdict: correct.**
`worker.py:1035` built `RunResult(..., 0, 0, 0)` and `:557` assigned reservation, spend and
attempts, so cancelling a job that had been killed after a paid primary checkpoint reported zero
spend for money the owner was really charged.
*Fix:* `_cancel_before_start` recovers reservation, spend and attempts from the run row *and* the
`primary` checkpoint state, and `terminal()` settles with `MAX(...)` because money and attempts are
monotonic within a run (§2.3.2). *Test:*
`test_cancelling_a_resumed_paid_job_keeps_its_reservation_spend_and_attempts`.

**P1-4 — intake had no heartbeat and none of the pipeline's locks. Verdict: correct.**
The heartbeat thread started at `:975`, after fetch, decode and hints at `:703`.
*Fix:* one heartbeat context now spans the whole claim, and `_prepare_intake` takes the pipeline's
own source and media `ProcessLock`s (same keys, same order) and releases them before `service.run`
re-acquires them. A source another process is holding is *deferred* — attempt returned, cooldown —
never failed. *Tests:* `test_intake_holds_its_heartbeat_and_the_source_and_media_locks`,
`test_a_busy_source_defers_intake_without_burning_an_attempt`.

**P1-5 — expired ownership stayed valid. Verdict: correct.**
`:487`, `:508` and `:520` omitted lease expiry, and fencing was by worker identity.
*Fix:* `heartbeat`, `update_progress` and `begin_analysis` require the current claim token **and**
`lease_until > now`; `cancel_requested` reports "stop working" for a missing row, a rotated token or
an expired lease. *Test:*
`test_an_expired_lease_cannot_be_renewed_or_used_even_by_the_same_worker_id` (the replacement
deliberately reuses the first worker's id).

**P1-6 — breaker-waiting jobs never resumed. Verdict: correct, reproduced.**
`wait()` stored `{"reason": ...}` (`:610`) and the claim predicate evaluated
`NOT (state='waiting' AND json_extract(progress,'$.attached')=1)`; with the key absent that is
`NOT (1 AND NULL)` = NULL, so SQLite excluded the row forever.
*Fix:* the predicate is NULL-safe (`COALESCE(...)=1`), `wait()` writes an explicit `attached` key,
returns the attempt it consumed (waiting is not a failed attempt) and holds the row unclaimable for
a cooldown using `lease_until` with no owner. *Test:*
`test_a_breaker_waiting_job_resumes_without_exhausting_its_attempts` — four waits, still completes
on attempt 1.

**P1-7 — dead letters left immortal active runs. Verdict: correct.**
`:408`, `:526` and `:614` changed only `jobs`, so a dead-lettered job left its run in `analysis`
and later submissions attached to it at `:860`.
*Fix:* dead-lettering (from `claim` and from `fail`) and the 24-hour expiry move the run to the same
fate, preserving attempts, reservation and spend; the attach lookup now requires a run that a
non-terminal job is still driving. *Tests:* `test_a_job_dead_letters_on_its_third_failed_attempt`
(run status) and `test_a_dead_lettered_run_is_never_attached_to_and_expires_to_failed`.

**P1-8 — one invalid row stopped the consumer. Verdict: correct.**
`_row()` raised inside the claim transaction (`:426`), rolling back the attempt increment and
escaping `run_once()`'s handler (`:694`).
*Fix:* unparseable rows are quarantined as dead letters inside the same transaction (with their
run) and the claim moves on; `run_forever` additionally survives any per-job exception.
*Tests:* `test_one_unreadable_row_is_quarantined_and_the_consumer_carries_on` and the tampered
`LocalPath` target test.

**P1-9 — Shazam attempt events were missing. Verdict: correct.**
`:993` built only an AudD journal, so `provider_attempt_events` could never supply §2.3.5's Shazam
denominator to 4b-ii.
*Fix:* `LedgerShazamBreaker`, a `ShazamBreaker` subclass injected as the pipeline's breaker, writes
`prepared` when an attempt is admitted (before network I/O) and `dispatched` + `resolved` when it
settles; `SQLiteAttemptJournal.backfill()` replays the durable JSONL into SQLite before each pass,
repairing a crash between the append (`:271`) and the insert (`:274`). *Tests:*
`test_shazam_attempts_reach_the_ledger_and_are_fenced`,
`test_attempt_projection_is_backfilled_from_the_durable_journal`, and the real-process gate now
asserts Shazam resolved rows beside the 21 AudD rows.

**P1-10 — retained compatible results could fail intake. Verdict: correct.**
`:736` accepted `_load_cached` and then decoded unconditionally at `:738`, so once retention had
pruned the PCM and the original a perfectly servable stored bundle dead-lettered.
*Fix:* intake mirrors `pipeline.py:543-557` — a retained bundle's manifest supplies `duration_ms`,
no decode runs, and the `decode` checkpoint is claimed only when this pass really decoded.
*Test:* `test_retained_result_intake_serves_without_decoding_a_pruned_media` drives the **real**
resolver with the PCM and original absent and fails the test if anything calls `decode`.

**P1-11 — tenant identity differed between intake and execution. Verdict: correct.**
Intake used `job.tenant_scope` (`:760`) while `_store()` handed the pipeline unchanged options
(`:727`) that `service.py:534` reads, so a `user:alice` job produced two different compatibility
scopes. *Fix:* `_options_for(job)` puts the job's scope into the options every store and request
carries. *Test:* `test_intake_and_the_pipeline_agree_on_the_job_tenant_scope`.

**P1-12 — migration state was read without cross-process serialisation. Verdict: correct.**
`migrate()` read `schema_migrations` outside any lock, and `executescript` commits, so SQLite's own
write lock cannot span the decision. *Fix:* a blocking `flock` / `msvcrt.locking` lock file
(`<db>.migrate.lock`) wraps read-and-apply. *Test:* `test_simultaneous_migrators_are_serialised`
(four concurrent migrators on separate handles; all succeed, exactly one migration applied).

**P2s.** The unused manual-tracklist duplicate (`:798`) is gone — intake passes no manual tracklist
and records the empty hash the identity expects. `source_has_no_http_framework()` no longer lives in
the module; the test now scans **every** file under `src/idea_web`, not just `worker.py`.

**Found beyond sol.** The SQLite projection used `INSERT OR IGNORE`, which silently swallows CHECK
violations as well as duplicates — the first backfill implementation wrote the JSONL's 0-based
`seq` and every `prepared` row vanished with no error. The projection now uses
`ON CONFLICT(attempt_id, seq) DO NOTHING` and maps the event kind to the ledger's 1..3 sequence.
Also: a job deferred *during* intake must be parked back in `intake`, not `waiting`, or the next
claim takes the analysis path for a job with no run to resume.

### Independent checks that were asked for

- **Can one job ever run twice?** Claiming is one `BEGIN IMMEDIATE` transaction whose `UPDATE`
  repeats the expiry predicate, so two workers cannot both claim. Wall-clock leases remain
  vulnerable to clock skew or a sleeping machine — inherent to leases — but the money consequence is
  now closed: a paid request is journaled (`prepared`) before dispatch and the journal is fenced, so
  the loser of a reclaim raises `StaleClaim` **before** any AudD request leaves the process, and can
  neither checkpoint, settle nor move the job. Asserted by the P0-1 test.
- **Is `provider_attempt_events` genuinely append-only?** `BEFORE UPDATE` and `BEFORE DELETE`
  triggers abort every mutation (both asserted), and no `UPDATE` or `DELETE` against the table
  exists anywhere in `src/`. Inserts tolerate a duplicate but never overwrite one.
- **SQLite discipline.** Every connection sets WAL, `foreign_keys = ON` and a 5 s busy timeout
  (asserted); writes go through one process-local writer lock plus `BEGIN IMMEDIATE`; no transaction
  is held across pipeline work — `service.run` runs outside every transaction and the heartbeat is
  its own short write. The only file I/O inside a write transaction is the intake decision's
  bundle-manifest read, bounded by the bundles of a single `media_key`.
- **Do the `down` migrations really reverse?** The test now compares the whole of `sqlite_master`
  (tables, indexes *and* triggers) with `{schema_migrations}` after `migrate(0)`; a no-op `down`
  would leave five tables, four indexes and two triggers, and fail.
- **Local mode untouched.** Nothing under `src/id_detector/` changed (`git status`), `id_detector`
  never imports `idea_web` (so local mode still needs no SQLite), `webapp/jobs.py` is untouched, and
  `smoke_serve.ps1`, `gate_local_mode.ps1` and `test_stage10_webapp.py` pass unchanged.

### Final lease, claim-token and durability semantics

- **Claim.** One `BEGIN IMMEDIATE` transaction takes the oldest row in `intake|waiting|analysis`
  whose `lease_until` is NULL or past — skipping attached subscribers, dead-lettering rows that have
  exhausted `max_attempts = 3` and quarantining rows it cannot parse — and sets `lease_owner`,
  `lease_until = now + lease`, `heartbeat_at`, `attempt + 1` and a fresh **claim token** (a claim
  generation, not a worker id), rotating that token onto `analysis_runs` when the job has a run.
- **Fence.** Every checkpoint, money, attempt-event, progress, heartbeat, `begin_analysis`, `wait`,
  `fail` and `terminal` write requires the current claim token; renewals, progress and the cancel
  check additionally require an unexpired lease. A refused write raises `StaleClaim`.
- **Release.** `terminal`, `wait` and `fail` clear the job's token; terminal settlement also clears
  the run's, so a settled run accepts no further writes from anybody.
- **Heartbeat.** 10 s against a 30 s default lease, covering the whole claim including intake, and
  stopping as soon as the lease is lost.
- **Waiting.** A provider wait or a busy source returns the attempt it consumed and parks the job
  (`waiting`, or `intake` when no run exists yet) unclaimable for a 30 s cooldown.
- **Durability.** A checkpoint commits only after every artefact it names exists **and** has been
  fsynced (plus a directory fsync on POSIX). Reservation, spend and attempts on a run only ever
  move upwards.

### Left as P2 notes (not done, with the reason)

- **The decoder still does not fsync its own publication.** `decode.py:139` renames an ffmpeg
  temporary with `os.replace`; the checkpoint boundary now flushes those bytes, but on Windows the
  *rename* cannot be flushed from here (there is no directory fsync — `io.fsync_directory` is a
  deliberate POSIX-only no-op and atomic writers use `MoveFileEx WRITE_THROUGH` instead). Fixing the
  producer means editing `src/id_detector/decode.py`, which §4.2's rules line puts outside this
  cycle's allowed surface. Recommended as a one-line change for the cycle that owns the decoder.
- **A job may wait indefinitely** while a provider stays open-circuit, because waiting deliberately
  consumes no attempts; the operator view of long-waiting jobs belongs to 4b-ii.
- **Shazam ledger rows carry `query_id = NULL`.** The recognise path exposes only the process
  breaker to a host, and it knows the egress and the outcome but not the clip cache key; inventing
  one would be a lie. The column is nullable with a CHECK that AudD rows always carry one. Per-clip
  Shazam attempt identity means changing `recognise.py`, again outside this cycle.
- **Credits and USD reservations are still unimplemented** (`ReservationSeam` only): 4d-i owns them,
  and faking one here would be scope creep.

### Gate outputs after the fix pass

`uv run pytest -q`

```text
........................................................................ [ 98%]
..............                                                           [100%]
1166 passed, 93 deselected, 1 warning in 423.28s (0:07:03)
```

(1152 before this pass; the 14 new tests are the regressions listed above.)

`uv run pytest tests/idea_web/test_worker.py -q`

```text
27 passed, 1 warning in 15.81s
```

`uv run pytest tests/test_service_api.py tests/test_phase0a_money.py tests/test_phase0a_status.py tests/test_phase0a_crash_cache.py tests/test_phase0b_attempts.py tests/test_phase1a_compat.py tests/test_phase1b_breaker_scorer.py tests/test_phase2b_retention.py tests/test_phase3a_honesty.py tests/test_playlists.py tests/test_golden_local_free.py tests/test_stage10_webapp.py -q`

```text
293 passed, 1 warning in 214.98s (0:03:34)
```

`uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 52 inline scripts across 21 page renders
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
282 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 452 files
fixture audit passed
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-8fba9ec45ad94b07bb572c286aa97b99\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\443a4e2aa6e0c2c171b0954b37d7b7d645abe60d98491a5f634737be9ea1ead9\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-8fba9ec45ad94b07bb572c286aa97b99
```

`PYTHONIOENCODING=utf-8 uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out "$TEMP/4b-i-check.json"`

```text
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000; report=C:\Users\natha\AppData\Local\Temp\4b-i-check.json
```

Unchanged from the expected release-1 pooled numbers (likely 8971, recall 6422): this cycle touches
no scoring path.

`uv build` (packaging sanity for the new SQL package data, not a required gate)

```text
Successfully built dist\id_detector-0.1.0-py3-none-any.whl
idea_web/database.py · idea_web/jobs/worker.py · idea_web/migrations/0001_durable_queue.up.sql ·
idea_web/migrations/0001_durable_queue.down.sql  (the migrations ship with the wheel)
```

`git status --short -- work data`

```text
```

(empty, as is the protected-file check for `src/id_detector/playlists`, `tests/test_playlists.py`,
`README.md`, `idea.cmd` and `src/id_detector/present/theme.py`; `git diff HEAD` over tracked files
is empty because every deliverable is still a new, untracked file.)

Nothing was committed, branched or pushed, and no CLI or pipeline invocation reached a live
provider: every run used `IDEA_TEST_MODE=1` with the repository's fakes.

REVIEW: OK_TO_COMMIT
