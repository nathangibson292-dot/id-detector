# Build review — 4b-ii (progress, operations, shared breaker) + 4b-iii (backups and restore)

Two adjacent cycles built together on `08ec7ab`. Nothing committed, no branch created, no gate script
run, no server started, no live provider call.

## What changed, file by file

### New — 4b-ii

- **`src/idea_web/migrations/0004_shared_breaker.up.sql` / `.down.sql`** — `provider_breaker_state`
  (one row per `(provider, egress_id)`): the open deadline, the day's count of rule-(a) opens, the
  rule-(c) latch and the operator's re-enable generation. Nothing that can be *counted* is stored
  here; the ledger is the count. The `down` drops the table and is exercised by a real
  up-to-latest-then-down test.
- **`src/idea_web/breaker.py`** — `SharedShazamBreaker`, a drop-in `ShazamBreaker` that measures
  §2.3.5 over the append-only `provider_attempt_events` instead of one process's memory:
  - **(a)** resolved Shazam attempts on that egress in the rolling `window_seconds`, minimum sample
    20, failure rate **>** 30 % → open for `cooldown_seconds`; an open cooldown is never re-tripped;
  - **(b)** `shazam_daily_budget_per_egress` against that egress's `prepared` rows for the UTC day;
  - **(c)** three (a)-opens in one UTC day → latched until an operator re-enables. A new UTC day
    resets the *count* of opens, never the latch (D8).
  The refusal strings are the process breaker's, unchanged (`shazam_breaker:a_failure_rate`,
  `:b_daily_budget`, `:c_latch`), as is the manual `IDEA_ENGINE_SHAZAM=off` kill-switch.
- **`src/idea_web/progress.py`** — `PageProgress`: turns the worker's `(phase, done, total, message)`
  ticks into the page's *own* job document by driving the shipped state machine (a real
  `webapp.jobs.Job` through a real `JobContext.progress`). One document, one renderer: `local.
  job_view` rebuilds a hosted job exactly as it rebuilds a local one.
- **`src/idea_web/ops.py`** — the operator's read-only view plus one action: queue depth by state,
  long-waiting jobs with their reason, dead letters with their cause, today's per-egress provider
  usage and its ledger cost, live breaker state, and free disk (R7). `reenable_breaker(...)` is
  §2.3.5 rule (c)'s manual half. Entry point `python -m idea_web.ops <app.db> [show|reenable]`.

### New — 4b-iii

- **`src/idea_web/backup.py`** — §4.6 step by step: take the artefact lock → `sqlite3.Connection.
  backup()` → enumerate every `bundle_id`/`run_id` **the copied database** names → hard-link those
  directories plus `recognise/`, `hints/` and `ingest/source.json` into `snapshot/artefacts/` →
  release. `snapshot.json` records the database hash, every listed artefact and the manifest hashes.
  `verify_snapshot` proves every listed artefact exists and hashes as recorded; `restore` does
  database → artefacts → `verify_artefacts` (bundle manifests, fuse-run manifests, completion
  sidecars). `describe`/`seal` seal a snapshot whose artefacts arrived by another route. Entry point
  `python -m idea_web.backup backup|verify|restore`.
- **`tests/fixtures/snapshot/`** — the committed restore drill (14 text-only files): one media's
  sealed bundle, its frozen fuse run, `recognise/` and `hints/` with their completion sidecars,
  `ingest/source.json`, the queue database as `app.sql`, and a `README.md`.

### Modified

- **`src/idea_web/jobs/worker.py`** (+75/−6) — four wirings, no restructuring:
  1. the worker's one breaker is now `SharedShazamBreaker` (a caller-supplied breaker still wins);
  2. §2.3.5's "open → new free jobs `waiting`": a free job that has **not started** is parked with
     the breaker's reason and gives its attempt back; a job already running is never interrupted;
  3. the progress callback publishes the page document beside the existing phase counters;
  4. the document is published **once before the first phase** and again at settlement, so a run
     that fails immediately is still visible with its cause (U-F33).
- **Six existing `tests/idea_web/` files** — mechanical: a fourth migration makes the schema version
  4, so `migrate() == 3` / `version() == 3` / `[3, 3, 3, 3]` / `applied == 3` / the applied-migration
  list became 4. Intermediate assertions (`version() == 1`, `== 2`, mid-migration targets) were left
  alone, as was `money_authority == 3`.
- **`tests/idea_web/test_followup_queue_money.py::test_one_breaker_state_spans_sequential_jobs_in_a_worker`**
  — one *behavioural* update. It still proves one breaker state spans sequential jobs in a worker;
  since 4b-ii the second job acts on it sooner, because §2.3.5 parks a new free job while the
  breaker is open instead of starting it. The refusal is now asserted on the queue row rather than
  inside the run.

## Tests added

`tests/idea_web/test_ops.py` (17): the breaker is shared between processes, not owned by one; rule
(a)'s minimum sample and its strict `>` threshold; rule (b) per egress and per UTC day; rule (c)
latching, surviving a day roll, and cleared only by an operator; a configured `reenable_generation`;
the manual kill-switch; migration 0004 up and down; an open breaker parks a new free job at zero
attempt cost and the job runs once it closes; an open breaker never interrupts a run already under
way; the queue publishes the page's own progress document (with U-F9's arithmetic proved to be the
shipped one); the hosted and local documents are the same shape; a failed queued run stays visible
with its cause; the operations snapshot; the operations entry point.

`tests/idea_web/test_backup.py` (10): every referenced artefact is listed, hard-linked and verifies,
and a removed link is caught; a backup during a bundle commit takes the same `<media>/.media.lock`,
refuses while it is held and verifies once it is released; **GC skips its cycle rather than waiting
for that lock, and the same lock is what makes a backup wait**; a backup never copies a corpus file;
the restore drill runs from the committed fixture; restore refuses while `idea serve`'s supervisor
holds the lock (and writes nothing); restore refuses a snapshot that does not verify; a restore
never writes a corpus file; `verify_artefacts` catches a damaged manifest and a damaged sidecar that
a file listing would miss; the entry point.

All offline and deterministic. No new dependency. Nothing is marked `live`.

## Plan ambiguities, and how I read them

1. **"The artefact lock" already exists.** §4.6 wants a lock "shared with GC and bundle commits",
   with "GC skipping a cycle rather than waiting". That is `<media>/.media.lock`: the pipeline holds
   it across ingest, journalling and publication, and `retention.collect` already records
   "media is active" and moves on when it cannot take it. Backup takes the same lock. **This is why
   `src/id_detector/` has a zero-line diff** — no new lock primitive was needed.
2. **A backup waits, then refuses.** `ProcessLock` is non-blocking, so backup retries for
   `lock_timeout` (30 s) and then refuses by name rather than snapshotting a half-published tree.
3. **Enumerate, lock, copy, re-enumerate.** A media that only the *copy* references (a bundle
   committed between the plan and the copy) is locked before anything of it is read.
4. **The daily budget is `max(ledger rows, this process's admissions)`.** The ledger's `prepared`
   row is written by the journal a moment after admission, and a caller that bypasses the journal
   writes none at all. Both counters count the same requests and another process's rows are only
   ever *more*, so the larger is the honest total: exact for one process, raised by every other.
5. **Operations ships no HTTP route.** Accounts and sessions are 4c-i and `/admin` is 6b; an
   unauthenticated operations endpoint would be a hole. The data and the action live behind the
   process boundary the worker already runs in.
6. **Sidecar upstream is verified only where it is still present.** Retention prunes upstream
   artefacts on purpose and rewrites the sidecars it orphans, so a missing upstream is not damage; a
   *present* upstream that hashes differently is.
7. **The fixture is text-only.** `scripts/audit_fixtures.py` refuses any committed fixture path with
   six consecutive digits, and Windows' 260-character limit rules out two 64-hex keys under
   `tests/fixtures/`. So: short source/media keys, the database as `app.sql`, and a run id chosen so
   that the content-addressed bundle directory (`sha256(run_id, presentation_version)`, independent
   of `PAGE_VERSION`) carries no six-digit run. The drill seals the snapshot with the production
   `describe`/`seal`, so what it restores is a real snapshot.

## U-F9 — the owner's weighting is DEFERRED

**Nothing in this build changes how progress is weighted or estimated.** The owner wants 10 % of the
bar to mean about a tenth of the expected wall-clock time and has not approved changing it, so
`PHASE_EXPECTED_SECONDS`, `Job.progress_percent`, the observed-rate recognise estimate and the
monotonic clamp are untouched. 4b-ii only makes the *durable queue* publish that same shipped
calculation. `test_ops.py` pins this twice: the document's percentage is reproduced by rebuilding
the page object and asking it again, and the new module is asserted to define no weights of its own.
**The re-weighting remains parked pending the owner.**

## `PAGE_VERSION`

**No bump needed.** This cycle renders no page markup, changes no static asset and touches no
template — `src/id_detector/present/theme.py` and every other protected file are untouched.

## Money

Untouched. `run_reservations`, `run_dispatches`, `run_settlements`, the claim and cancel fence and
the settlement sweep are unchanged; migration 0004 is the next number, additive, has a working
`down`, and goes through `Database.migrate()` unchanged (atomic, under the supervisor lock for a
local work root). Hosted paid dispatch is still refused before the new breaker check is reached.
`ops.provider_usage` reads the attempt ledger for an operator's eyes and is documented as *not* the
money authority.

## Gate outputs

`uv run pytest tests/idea_web/test_ops.py -q` and `uv run pytest tests/idea_web/test_backup.py -q`
(run together):

```text
........................                                                 [100%]
24 passed, 1 warning in 10.53s
```

Full suite in two shards that together cover every collected file
(`uv run pytest --collect-only -q` → `1751/1848 tests collected (97 deselected)`;
250 + 1499 passed + 2 skipped = 1751):

```text
$ uv run pytest tests/idea_web -q
250 passed, 1 warning in 216.14s (0:03:36)

$ uv run pytest tests --ignore=tests/idea_web -q
1499 passed, 2 skipped, 97 deselected, 1 warning in 794.25s (0:13:14)
```

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
365 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 504 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```text
$ git status --short
 M src/idea_web/jobs/worker.py
 M tests/idea_web/test_followup_queue_money.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_worker.py
?? src/idea_web/backup.py
?? src/idea_web/breaker.py
?? src/idea_web/migrations/0004_shared_breaker.down.sql
?? src/idea_web/migrations/0004_shared_breaker.up.sql
?? src/idea_web/ops.py
?? src/idea_web/progress.py
?? tests/fixtures/snapshot/
?? tests/idea_web/test_backup.py
?? tests/idea_web/test_ops.py

$ git diff --stat
 src/idea_web/jobs/worker.py                 | 81 ++++++++++++++++++++++++++---
 tests/idea_web/test_followup_queue_money.py | 12 +++--
 tests/idea_web/test_followup_round2.py      |  2 +-
 tests/idea_web/test_followup_round6.py      |  4 +-
 tests/idea_web/test_followup_round7.py      |  2 +-
 tests/idea_web/test_followup_round8.py      |  8 +--
 tests/idea_web/test_followup_round9.py      | 13 ++---
 tests/idea_web/test_worker.py               |  6 +--
 8 files changed, 102 insertions(+), 26 deletions(-)
```

`git status --short` for `src/id_detector/playlists`, `tests/test_playlists.py`, `README.md`,
`idea.cmd`, `src/id_detector/present/theme.py`, `profiles/`, `docs/PLAN-v2.md`, `work/` and `data/`
is empty: none of them was touched. **`src/id_detector/` has a zero-line diff.**

## What I could not do, and why

- **`idea backup` is not yet spelled that way.** §4.6 names the command `idea backup`, but its
  subcommand would live in `src/id_detector/cli.py`, which §4.2 freezes after Phase 3 (only
  `service.py`, `recipes.py`, the `serve` entry point and `PROJECT_ROOT` are open). The whole
  behaviour ships as `python -m idea_web.backup`; **binding the `idea backup` / `idea restore` /
  `idea verify-artefacts` names needs about three lines in `cli.py` and an owner's decision to open
  that surface.** Recommended for whoever owns the cycle that may touch the CLI, or for 6a-iv.
- **Step (6), the upload, is not implemented.** §4.6's own phasing puts the off-box copy and the
  nightly schedule in 6a-iv ("Litestream + nightly `idea backup`"); this cycle produces and verifies
  the snapshot that step uploads.
- **The PowerShell gates were not run** (`smoke_serve.ps1`, `gate_local_mode.ps1`) and no server was
  started: the orchestrator runs those. Nothing here changes headers, routing or page serving, and
  `tests/idea_web/test_headers_forms.py` — which pins `frame-ancestors 'self'` with
  `X-Frame-Options: SAMEORIGIN` and `frame-src 'self'` — passes untouched.
- **Shazam ledger rows still carry `query_id = NULL`** (4b-i's note). The shared breaker does not
  need a per-clip identity: its denominator is attempts on an egress, not on a clip.

---

# Fix pass — review verdict FIX_FIRST (3 P0, 7 P1, 2 P2)

Every finding below was reproduced before it was fixed, and each fix has a regression that fails if
the fix is reverted. Two of them were design errors, not patches, and were done first.

## A. A backup must never wait behind a running scan (B2 / fix 4)

**What was wrong.** I reused `<media>/.media.lock` — correct, it *is* the shared artefact lock — but
waited on it for 30 seconds. The pipeline holds that lock for the whole of an analysis, so a nightly
snapshot taken during any real scan simply refused. §4.6's intent is that the backup coexists with
work and that *GC* is the one that gives way.

**What it is now.** The lock is taken **per medium, without waiting**, and held only for that
medium's own linking — seconds, never the length of a run. A medium whose lock is held is skipped
deliberately and recorded in `snapshot.json` under `skipped`, with its reason and the bundle and run
ids it was therefore not carrying. A skipped medium is not a failure, but it is never invisible.
Nothing half-written is ever captured: what a snapshot carries is only what is **published** —
sealed bundles and frozen fuse runs that verify — and an unsealed, in-flight run is left behind.

*Test:* `test_a_backup_coexists_with_a_scan_and_records_the_busy_medium_as_skipped` — a scan holds
one medium while GC runs and the backup is taken; the backup completes, lists every published
artefact of the idle medium, records the scanned one as skipped with its ids, and verifies.

## B. A sealed snapshot must be immutable (A2 / fix 2)

**What was wrong.** I hard-linked everything, including `recognise/*attempts.jsonl` — journals later
runs append to in place. A later scan of the same medium silently rewrote an already-sealed backup
and invalidated its own `snapshot.json`.

**What it is now.** Only **sealed, content-addressed** directories are hard-linked: a bundle and the
frozen fuse run its manifest names, whose bytes a manifest fixes. Everything appendable — the
`recognise/` and `hints/` trees and `ingest/source.json` — is **copied**.

*Test:* `test_a_sealed_snapshot_is_not_changed_by_a_later_scan` appends to the medium's journal after
sealing and asserts the snapshot's bytes and `snapshot.json` still verify unchanged, that the
journal copy has `st_nlink == 1` and the bundle's manifest `> 1`.

## The rest

1. **P0 completeness (A1 / fix 1).** `backup()` now refuses rather than seals when a bundle the
   copied database names is outside the work root, missing, or does not verify — and it pulls in and
   verifies the frozen fuse run that bundle's manifest names. The dead `directories` variable in
   `verify_artefacts()` became the check it was meant to be: every sealed directory in the listing
   must have a recorded manifest. *Tests:* three refusal tests (missing bundle, outside the work
   root, manifest that does not describe its files).
2. **P0 restore safety (B1 / fix 3).** Restore now stages into `.idea/restore-staging-<token>/`,
   fsyncs every file and its directory entry, **verifies the staged tree semantically**, and only
   then publishes file by file with `os.replace`, keeping every replaced file aside; any failure
   rolls the previous tree back. The supervisor lock is held across all of it, verification
   included, and existing `-wal`/`-shm` are moved aside so a stale WAL cannot be replayed into the
   restored database. *Tests:* the semantic-refusal test (below), a forced mid-publication failure
   that must leave the original file byte-identical, and a live-WAL replacement test.
3. **P1 breaker rule (b) (A4 / B4 / fix 5).** Counts `dispatched`, never `prepared` — §2.3.3 is
   explicit that prepared-without-dispatched was never sent, so a cancelled or refused attempt no
   longer spends the egress's daily budget. The in-process counter moved from `dispatch()`
   (admission) to `sent()` (entering network I/O), and `release_dispatch()` is correctly a no-op.
   `resolved()` no longer judges — the caller writes the row *after* it returns — so judging happens
   at the next admission, against committed rows. Open/latch updates are decided **inside one write
   transaction that re-reads the row**, so two workers tripping at once cannot lose a trip.
4. **P1 ops is read-only (A5 / fix 6 / B3).** `breaker_states()` uses the new
   `SharedShazamBreaker.view()`, which judges exactly as `refusal()` does and **persists nothing**,
   and it judges with the worker's *configured* policy rather than library defaults. A configured
   `reenable_generation` is now compared against the **persisted** generation, so an operator's
   re-enable survives a restart. *Tests:* looking at a breaker that would trip writes no row and the
   default policy reports differently from the configured one; and a re-enable applied by a
   brand-new object after a "restart".
5. **P1 progress coverage (B5 / fix 7).** One page document now spans the whole claim: it is built
   at claim time **resuming the previous attempt's document**, published before intake, and carried
   through the cache-hit and attachment branches and into analysis and settlement. Retries keep
   measured `phase_seconds` and the monotonic `progress_max` instead of resetting the clock.
   *Tests:* intake failure stays visible, cache hit and attachment each carry the document, and a
   retry never moves the bar backwards.
6. **P1 Deep labelling (B6 / fix 8).** `_page_profile()` maps a paid recipe to `max_accuracy`, the
   only profile the frozen renderer treats as paid, and `_result_path()` publishes a real
   work-root-relative result route ending in `index.html` instead of a bare bundle id. Both live on
   the `idea_web` side; `src/id_detector/present/server.py` is untouched.
7. **P1 verifier (A3 / fix 9).** `python -m idea_web.backup verify` now runs `verify()` — snapshot
   hashes **plus** bundle, fuse-run and sidecar checks — and returns a non-zero exit code on any
   failure. *Test:* damage only artefact verification can see, with hashes re-sealed to agree, must
   exit 1.
8. **P2 (fix 11).** The operations cost counts only `money.BILLABLE_OUTCOMES`: a 429, an auth error
   and a quota error cost nothing and are no longer billed to the display. `dispatched` is reported
   beside `prepared`. The dead verifier variable is gone.
9. **P2 (fix 12).** `safe_entry_path()` and `contained()` validate every snapshot entry as a
   normalised relative POSIX path confined beneath both `artefacts/` and the work root, rejecting
   absolute paths, drive letters, backslash separators, `..`, empty segments and any component that
   is a link or reparse point.

## The test the reviewer asked for specifically

`test_restore_refuses_a_semantically_broken_snapshot_and_keeps_the_previous_tree` is the one that
fails if restore's internal verification is removed: every hash in that snapshot agrees with its own
listing (so `verify_snapshot` passes), but a bundle's manifest no longer describes its files. Restore
must refuse **before publishing**, and a file already in the work root must survive byte-identical.

## Existing tests changed, and why

- `test_one_breaker_state_spans_sequential_jobs_in_a_worker` now calls `breaker.sent()` between
  `dispatch()` and `resolved()`. That is the contract, not a weakening: the budget moves when a
  request really leaves.
- `Worker._commit_intake`'s `tracker` is optional, so the two 4b-i tests that call it directly are
  untouched; when it is absent the method builds one from the job's stored document.
- No other assertion was weakened. Two of my own new assertions were corrected rather than the code:
  a sub-millisecond fake run legitimately computes 0 %, so the retry test asserts carried
  measurements and monotonicity instead of a positive percentage; and GC only walks 64-hex media
  names, so the "GC gives way" assertion lives in the test that uses one.

## Fix-pass gate outputs

Full suite in four foreground shards that together cover every collected file
(`uv run pytest --collect-only -q` → `1766/1863 tests collected (97 deselected)`;
265 + 602 + 429 + 468 passed + 2 skipped = 1766, and 25 + 72 = 97 deselected):

```text
$ uv run pytest tests/idea_web -q
265 passed, 1 warning in 248.02s (0:04:08)

$ uv run pytest <31 files: test_acrcloud_clip … test_projection> -q
602 passed, 1 warning in 496.08s (0:08:16)

$ uv run pytest <27 files: test_scan … test_stage4a_relations_fusion> -q
429 passed, 25 deselected, 1 warning in 145.12s (0:02:25)

$ uv run pytest <27 files: test_stage4b_transforms_schedule … test_truth_review> -q
468 passed, 2 skipped, 72 deselected, 1 warning in 149.15s (0:02:29)
```

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
366 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 506 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```text
$ git diff --stat
 src/idea_web/jobs/worker.py                 | 180 ++++++++++++++++++++++++++--
 tests/idea_web/test_followup_queue_money.py |  15 ++-
 tests/idea_web/test_followup_round2.py      |   2 +-
 tests/idea_web/test_followup_round6.py      |   4 +-
 tests/idea_web/test_followup_round7.py      |   2 +-
 tests/idea_web/test_followup_round8.py      |   8 +-
 tests/idea_web/test_followup_round9.py      |  13 +-
 tests/idea_web/test_worker.py               |   6 +-
 8 files changed, 197 insertions(+), 33 deletions(-)

$ git diff --stat -- src/id_detector
(empty: src/id_detector still has a zero-line diff)
```

`git status --short` for `src/id_detector/playlists`, `tests/test_playlists.py`, `README.md`,
`idea.cmd`, `src/id_detector/present/theme.py`, `profiles/`, `docs/PLAN-v2.md`, `work/` and `data/`
is empty. Nothing was committed or branched, no PowerShell gate was run, no server was started, and
no CLI or pipeline invocation reached a live provider.

## Still deferred after the fix pass

The two accepted scope decisions stand: the command is `python -m idea_web.backup` rather than
`idea backup` (that spelling needs ~3 lines in the §4.2-frozen `cli.py`), and step (6), the off-box
upload and nightly schedule, belongs to 6a-iv. One new note: a job parked by the breaker still has
its page document replaced by `JobQueue.wait()`'s `{"attached", "reason"}` payload, so a *waiting*
job is not renderable as a card. Fixing that means changing 4b-i's `wait()` contract, which is
outside this cycle's scope; recorded here for whoever owns it.

---

# Second fix pass — round-2 review, four realistic P0s

Round 1 was patched instance by instance, and round 2 found new instances of the same two classes:
destination/write safety, and publication atomicity. So this pass changes the **shape** of both
operations rather than adding another check.

## DESIGN 1 — a backup writes only into a staging directory it owns, and publishes once

**What was wrong.** `backup()` wrote directly into the caller's directory: it deleted that
directory's `app.db` before opening the source, overwrote listed files, and rewrote `snapshot.json`
in place. `--into work/.idea` therefore **deleted the live money-authority database** — a realistic
path-selection accident, and a P0 no bolted-on check could make safe.

**What it is now.**

- The destination may not be, contain, or sit inside the work tree **or the source database's
  directory**. That is checked before anything is opened, and it makes the `--into work/.idea`
  accident impossible by construction rather than by inspection.
- A destination that already holds a sealed `snapshot.json` is refused; a non-empty one is refused.
  Snapshots are never rewritten.
- Everything is assembled in `.<name>.staging-<token>` beside the destination and published with
  **one rename**. Until that rename the destination does not exist, so a crash anywhere in the
  build leaves it untouched and the staging tree is cleaned up.
- `snapshot.json` is written through a flushed temporary and renamed, never edited in place, so a
  crash during sealing cannot truncate the only manifest.

*Tests:* destination equal to / inside / containing the work root and equal to the database's own
directory; reuse of a sealed destination (with the sealed bytes asserted unchanged); and a crash
injected before publication, after which the destination does not exist and no staging tree remains.

## DESIGN 2 — restore replaces the tree as one recoverable boundary

**What was wrong.** Restore verified the snapshot *before* taking the supervisor lock, overlaid
files one at a time without removing anything the snapshot did not list, fsynced only the database's
parent, kept rollback state only in memory, and let `--database` point anywhere while the lock was
always derived from `--work-root`. The absolute bundle paths the worker stores were copied through
unchanged, so a snapshot restored into a different root pointed at the tree it came from.

**What it is now.**

- The supervisor lock is taken **first** and held across staging, verification and publication.
- `--database` must live inside the locked work root; anything outside is refused, so an unrelated
  live database can never be replaced without its own lock.
- The staged database's `result_bundles.path` values are **re-rooted** for where the snapshot is
  landing, using the source root now recorded in `snapshot.json` (falling back to the fixed
  `<source>/<media>/present/bundles/<id>` shape). Rewriting happens **before** publication, so what
  lands is already correct.
- Artefacts under the subtrees a restore owns (`present/bundles`, `fuse/runs`, `recognise`,
  `hints`) that the snapshot does not list are **removed**, not overlaid — local discovery walks the
  filesystem, so a newer bundle would otherwise stay visible after restoring an older snapshot.
- A journal is written and flushed before publication naming the staging tree, the database, the
  artefacts and what is being removed, and a `committed` marker is written after. Every renamed
  parent directory is fsynced.

*Tests:* the drill asserts re-rooting (`rerooted == 1`, the stored path resolves under the new
root); a snapshot restored into a **different** work root is then backed up again cleanly; a stale
unlisted bundle is gone afterwards; a `--database` outside the locked root is refused with the
outsider's bytes intact; a failure injected part way through publication leaves the previous file
byte-identical and no database published; and the publication journal's contents are asserted.

## The rest

1. **P0 completeness.** `_referenced()` now classifies every run by status: a **non-terminal** run
   is *always* skipped with its reason, even when its medium's lock is momentarily free, and a
   terminal run whose frozen artefacts are missing is recorded as skipped rather than silently
   omitted. Bundles remain a refusal.
2. **P1 linking and locks.** Only content-addressed **bundles** are hard-linked; frozen fuse runs
   are keyed by a caller-supplied `run_id`, so they are copied like the journals. Lock acquisition
   is a **single non-blocking attempt** — no grace, no retry loop — and the lock is released before
   any manifest verification or hashing, which now run against the staged copies.
3. **P1 breaker re-enable.** `provider_breaker_state` gained `reenabled_at`, and rule (a) ignores
   every sample at or before it. The process breaker cleared its deque; an append-only ledger
   cannot be cleared, so the cutoff is the equivalent. Without it a re-enable was undone by its own
   history on the very next judgement.
4. **P1 ops read-only.** Every read opens `file:…?mode=ro` — no file creation, no journal-mode
   change — and the breaker is judged through a new `view_with(connection)` that writes nothing. A
   missing database reports nothing instead of being brought into existence.
5. **P1 cache hits and spend.** `_compatible_bundle()` returns the **selected** bundle's stored
   path, which the cache-hit branch renders, so a link cannot 404 across two source keys sharing one
   media. Terminal settlement publishes `ceil_e2(RunResult.usd_e6_spent)` with `spend_known`, so a
   Deep page states its cost instead of "could not be read".
6. **P2 comments.** The stale "prepared" claims in `worker.py` and `0004_shared_breaker.up.sql` now
   say `dispatched`.
7. **Adversarial P2.** Link rejection uses `truth_paths.is_link`, the repository's existing test for
   symlinks *and* junction/reparse points, rather than `is_symlink()`. **Tested with a real
   junction** created by `mklink /J`, asserting first that `is_symlink()` does not see it.

## Tests added this pass

A genuine concurrency gate — a bundle commit holding a medium's lock **while** GC runs **and** a
backup is taken — asserting the backup finishes without waiting, lists the idle medium in full,
records the busy one as skipped, verifies, and that GC gave way. A timing assertion with four busy
media catches any per-medium wait (the old one-second grace would have cost four seconds). Plus the
regressions listed under each fix above.

One notable bug this pass found in my own code: the staging prefix pushed staged bundle paths past
Windows' 260-character limit, and a plain `Path.is_dir()` answers *False* there — so every bundle
read as "missing or does not verify". Deep-path existence tests now go through `native_path`, and
the staging suffix is short.

## Second fix-pass gate outputs

Full suite in four foreground shards covering every collected file
(`uv run pytest --collect-only -q` → `1778/1875 tests collected (97 deselected)`;
277 + 602 + 429 + 468 passed + 2 skipped = 1778, and 25 + 72 = 97 deselected):

```text
$ uv run pytest tests/idea_web -q
277 passed, 1 warning in 241.30s (0:04:01)

$ uv run pytest <31 files: test_acrcloud_clip … test_projection> -q
602 passed, 1 warning in 465.10s (0:07:45)

$ uv run pytest <27 files: test_scan … test_stage4a_relations_fusion> -q
429 passed, 25 deselected, 1 warning in 143.79s (0:02:23)

$ uv run pytest <27 files: test_stage4b_transforms_schedule … test_truth_review> -q
468 passed, 2 skipped, 72 deselected, 1 warning in 151.31s (0:02:31)
```

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
366 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 506 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

```text
$ git diff --stat -- src/id_detector
(empty: src/id_detector still has a zero-line diff)
```

`git status --short` for `src/id_detector/playlists`, `tests/test_playlists.py`, `README.md`,
`idea.cmd`, `src/id_detector/present/theme.py`, `profiles/`, `docs/PLAN-v2.md`, `work/` and `data/`
is empty. Nothing committed or branched, no PowerShell gate run, no server started, no live
provider call.

## Fixture change

`tests/fixtures/snapshot/app.sql` now stores an **absolute** `result_bundles.path` under the
placeholder root `/idea-fixture-work`, exactly as production does, and the drill seals that root as
`work_root`. The relative shortcut the reviewer flagged is gone, so the drill exercises re-rooting
rather than avoiding it. The fixture stays text-only and audit-clean.

## Deferred, by direction

The parked-job page document is **not** built. The reviewer confirmed the behaviour (a `404`, the
job absent from activity and not cancellable), and also that it is a Free job parked before intake
and reservation: no money is at risk, and the queue row with its reason stays durable and visible in
operations. Recorded as a follow-up for whoever owns `JobQueue.wait()`'s contract.

## Final handoff check (2026-09-16)

At the owner's request, Codex closed out this existing work in its original worktree without
starting another cycle or changing the implementation. The earlier sections are the builder's
chronological reports; this section records the final verification before committing.

- Focused offline tests: `test_ops.py`, `test_backup.py`, `test_worker.py`,
  `test_followup_queue_money.py`, and `test_followup_round{2,6,7,8,9}.py`: **152 passed**,
  one existing `audioop` deprecation warning, in 152.50 seconds.
- Ruff check and format check passed (366 files); fixture audit passed (506 files);
  `git diff --check` passed. `src/id_detector/` remains unchanged.
- The first sandboxed test attempt could not access pytest's temporary directory; it was
  stopped and rerun with the required filesystem access. The passing result above is that rerun.
- Updated `docs/STATUS.md`. The deferred items above remain deferred. No new review round,
  full-suite rerun, PowerShell browser gate, or live provider call was performed at handoff.

# Fix pass 3 (completed) — round-4 review, uncommitted on top of `6133f27`

A usage limit interrupted the third fix pass, and Codex committed the partial work as `6133f27`.
This pass finishes the round-4 items as **uncommitted edits** on top of that commit. Corpus
confinement, missing terminal media, the breaker cursor, page spend, junction roots, exclusive
staging and link counts were already done and were not touched. `src/id_detector/` is unchanged
(`git diff 08ec7ab -- src/id_detector` is empty).

- **P0: durable restore on Windows, recovered at startup.**
  - Every publication, rollback rename and journal write goes through `io.durable_replace`
    (MoveFileExW WRITE_THROUGH). Each rename first confirms its destination is inside the work
    root and is not a corpus path.
  - The journal lists every target with an `existed` flag, plus `database_existed`. Recovery
    removes every target that had no predecessor, and restores the kept files.
  - Recovery runs before the database is opened or migrated, in two places:
    - `local_database()`, via `recover_if_unowned`, which takes the supervisor lock without
      blocking. It needs to be here because the frozen `serve` builds `LocalJobs` before
      `start()`.
    - `LocalWorkerSupervisor.start()`, with the lock already held.
- **P0: stable capture.**
  - Size and mtime_ns of every member are recorded under the media lock, and each member is
    re-checked after its link or copy.
  - If a member changed or vanished, the staged files are discarded and the capture is retried
    once (`CAPTURE_ATTEMPTS = 2`). A medium that becomes busy on the retry is recorded as skipped.
    If it still changes, the backup is refused. No member is dropped silently.
  - Any other copy error refuses the backup.
  - Every copied file is fsynced, and so are the staged database and every staging directory
    (deepest first) before the single publish.
- **P1: stale replacement** now also covers `ingest/source.json`, `present/current` and legacy
  flat `present/*` files, which are captured as kind `present`.
- **P1: `pruned_upstream`** must match `[0-9a-f]{64}` exactly, as in the frozen verifier.
- **P1: sidecar upstream keys resolve against the media directory**, as retention resolves them,
  and the artefact glob goes through `native_path`.
  - **Deviation:** a snapshot never carries decoded audio, windows or original media. An
    upstream outside the owned footprint may therefore be absent without failing, but if it is
    present its hash must still match.
  - In-scope upstreams keep the frozen rule that a missing file is a failure.
  - An unsafe upstream key is reported, never resolved.
- **P1: operations read without `immutable=1`.**
  - If `-wal` and `-shm` both exist (a live writer), it opens `mode=ro` and uses
    `backup()` into memory.
  - Otherwise it copies the database (plus any `-wal`) into a temporary directory, checks the
    source's identity is unchanged, and reads the copy.
  - Either way it creates no sidecar next to the real database. A test proves this against a
    live WAL writer with `wal_autocheckpoint=0`.
- **P2: adversarial input.**
  - A snapshot that lists one path twice (compared case-insensitively) is refused.
  - Every journal target, the database path and every kept file is confined inside the work
    root before any parent is created. An unexpected kept file refuses recovery.
- **Regression tests added:**
  - GC and backup against a bundle commit in progress: `gc_actions` is asserted, and the backup
    contains the committing medium.
  - A child process is killed mid-publication, then `LocalJobs` + `supervisor.start()` recover
    the work root, including a publication with no predecessor.
  - Server start and a new restore each consume an interrupted journal.
  - A tampered journal cannot make recovery write outside the work root.
  - Capture mutation: a member changes once, keeps changing, or vanishes once; and a copy fails.
  - Nested directories are flushed.
  - Stale non-bundle media are removed.
  - Duplicate snapshot paths are refused.
- **Test fixed:** the operations snapshot test depended on today's date. It now passes a
  negative wait threshold.

## Fix pass 3 gate outputs (2026-09-17)

- `uv run pytest --collect-only -q`: **1792/1889 collected** (97 deselected).
- Every collected file ran in foreground shards, 1792 tests in total:
  - `tests/idea_web` + `tests/test_truth_gateway_guard.py`: 308 passed. That is 291 from
    `tests/idea_web`; the 17 guard tests also ran in a shard, so 291 + 651 + 850 = 1792.
  - Shard 0 (40 files): 649 passed, 2 skipped.
  - Shard 1 (39 files): 850 passed.
- Gates:
  - `uv run pytest tests/idea_web/test_ops.py -q`: 26 passed.
  - `uv run pytest tests/idea_web/test_backup.py -q`: 39 passed.
- `uv run ruff check .`: passed. `uv run ruff format --check .`: 366 files already formatted.
- `uv run python scripts/check_page_js.py`: passed (53 inline scripts, 22 page renders, plus
  app.js).
- `uv run python scripts/audit_fixtures.py`: passed.
- `git diff --check` is clean. No PowerShell gate, server, or live provider call was run.

# Fix pass 4 — round-5 review (four items open), uncommitted on top of `6133f27`

`src/id_detector/` is still unchanged (`git diff 08ec7ab -- src/id_detector` is empty), and the
protected paths are untouched. Every fix below has a regression. I reverted each fix on a scratch
copy (six mutations, the source restored afterwards) and its regression failed every time.

- **P0: no server during a restore.**
  - Ownership is explicit: a restore takes `.idea/restore.lock` first, then the supervisor
    lock, and holds both to the end. Its journal (the recovery tree) is the durable marker.
  - `local_database()` now calls `backup.recover_before_use()` before any `Database` is built.
    - If the restore lock is held, it refuses with "a restore is in progress; wait for it to
      finish".
    - If there is a journal and it can take both locks itself, it recovers.
    - If there is a journal but the supervisor lock is busy, it refuses.
    - It never passes silently.
  - `LocalWorkerSupervisor.start()` checks the restore lock when the supervisor lock is busy.
    If a restore holds it, `start()` raises `MigrationRefused`. The frozen `serve` ignores a
    `False` return, so raising is the only fail-closed answer.
  - Regression: a real restore runs in a child process, paused by a barrier after its first
    publication. `local_database()`, `LocalJobs(work_root)` and `supervisor.start()` each
    refuse. No database is created and the tree's size and mtime are unchanged. After release,
    the restore completes and the root opens normally.
- **P0: durable directory boundaries.** New `src/idea_web/durable.py`:
  - `flush_directory` opens the directory with `CreateFileW` (GENERIC_READ|GENERIC_WRITE,
    share-all, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS) and calls `FlushFileBuffers`. On
    POSIX it uses `fsync` on a directory fd.
  - `move_directory` uses `MoveFileExW` with `MOVEFILE_WRITE_THROUGH` and no replace, then
    flushes both parents.
  - `remove_file` deletes, then flushes the parent. `make_directories` flushes every newly
    created entry.
  - Where they are used:
    - Snapshot publication uses `move_directory`, and staging directories are flushed with the
      real flush.
    - Recovery deletions of targets with no predecessor use `remove_file`.
    - Restore renames flush both parents, and publication parents are created with
      `make_directories`.
    - Recovery durably rewrites the journal to `rolled-back` before deleting the tree, so a
      resurrected tree cannot delete files written later.
  - Regressions: the publication `move` is followed by a flush of the destination's parent,
    with `os.replace` and `os.rename` forbidden on Windows. Each recovery `remove` is
    immediately followed by a flush of its parent, and the journal says `rolled-back` when its
    tree is deleted.
- **P0: capture membership carries across retries.**
  - Every member seen by any attempt is remembered. If a later view lacks one, the whole
    medium is skipped with a recorded reason naming the file, and its partial capture is
    discarded.
  - The vanish regression now keeps the file gone. The earlier "comes back" case is kept as a
    separate test.
- **P1: exact sidecar exception.**
  - Only these upstreams may be missing, matched exactly: `decode/audio.pcm`,
    `decode/pcm.json`, `windows/windows.genN.jsonl`, `windows/genN/*.wav` and
    `ingest/original[.ext]`. Anything else must be present with matching bytes, or carry a
    valid pruned marker.
  - A malformed digest always fails: non-hex, uppercase, the wrong length, a non-string, or an
    unknown object.
  - `provider_configs/<name>` resolves beside the sidecar, where `recognise.py` stores it, and
    is digest-checked.
  - `local-cache/<connector>/<job>/result.json` evidence is sealed into the snapshot at that
    media-relative key, found by digest in the project's hint cache (`backup(...,
    hint_cache=)`, default `PROJECT_ROOT/data/local/hints`, read-only; `--hint-cache` on the
    entry point). If it cannot be found, or its bytes differ, the medium is skipped with a
    reason. In a live tree or a snapshot, missing evidence fails verification.
  - Because the flat result's sidecars name `fuse/*.json` and `enrich/acquire.json`, the
    captured and owned footprint now also covers `fuse/` (live files) and `enrich/`, plus the
    sealed `local-cache/`.
  - Sidecars inside `fuse/runs/` and `present/bundles/` are proven by their manifest, as the
    frozen `retention.py` treats them ("Their recorded hashes remain evidence").
  - `_kind_of` now classifies by position within the medium, not by substring.
  - Regressions use production shapes: the `hints/pipeline.py` pair (`ingest/source.json` +
    `local-cache/...`) and `recognise.py` queries (windows record + `provider_configs/...`),
    plus near-miss spellings that must fail.
- **Optional follow-ups (both trivial, so done):**
  - `operations_snapshot` pins one in-memory copy for all its sections, and a test proves a
    single copy.
  - The unused journal field `removing` is gone.

## Fix pass 4 gate outputs (2026-09-17)

- `uv run pytest --collect-only -q`: **1800/1897 collected** (97 deselected).
- Every collected file ran in foreground shards, 1800 tests in total:
  - `tests/idea_web` (299) + guard (17, which also ran in a shard): 316 passed.
  - Shard 0 (40 files): 649 passed, 2 skipped.
  - Shard 1 (39 files): 850 passed.
  - 299 + 651 + 850 = 1800.
- Gates: `test_ops.py` 27 passed; `test_backup.py` 46 passed.
- `ruff check .` passed; `ruff format --check .` reports 367 files formatted.
- `check_page_js.py` passed (53 inline scripts, 22 renders, plus app.js).
- `audit_fixtures.py` passed.
- `git diff --check` is clean.
- No PowerShell gate, server, live provider call, or write to `data/` or `work/`.

### Fix pass 4 addendum: skipped media are preserved (coordinator decision)

The item deferred above was not acceptable, and it is now fixed.

- **Skipped media are preserved.** A medium the snapshot lists under `skipped`, and holds no
  artefacts for, is left exactly as it is: nothing removed, nothing replaced. A skip recorded
  by bare media key covers any `<source>/<key>` directory that exists now.
  - The journal and result record it under `preserved` as
    `"preserved: not in this snapshot (<reasons>)"`.
- **Other media behave as before.**
  - Media the snapshot never mentions are still removed, listed in the journal's `removed` and
    the result's `removed_paths`.
  - Captured media are still replaced. A medium with artefacts in the snapshot counts as
    captured. *(Superseded by fix pass 5: a skipped run's own subtree inside a captured medium is
    now preserved too.)*
- **Regression:** one medium's lock is held during the backup, so it is skipped. Its files then
  change, the captured medium's journal changes, and a new medium appears.
  - After the restore, the skipped medium is byte-identical, the captured journal is back to its
    snapshotted bytes, and the new medium is removed.
  - Disabling the preservation filter makes the test fail.
- **Test updated:** `test_restore_replaces_stale_media_anywhere_in_the_work_root` used to demand
  removal of a skipped medium. It now asserts that the medium is preserved.
- **Results:**
  - `tests/idea_web`: 300 passed. Gates: 27 (ops) and 47 (backup) passed.
  - Ruff check and format are clean, and `git diff --check` is clean.
  - Only `backup.py` and its tests changed, so the full shards were not re-run. Collection is
    now 1801.

# Fix pass 5 — round-6 review (two P0s), uncommitted on top of `6133f27`

Items 2 to 4 were confirmed DONE and were not touched. `src/id_detector/` is still unchanged.

- **P0: startup holds restore exclusion until migration returns.**
  - `backup.restore_excluded()` is a context manager. It takes the restore lock, refuses a
    live restore, and recovers an interrupted one. It keeps the lock held for the whole block.
  - `local_database()` constructs **and migrates** the database inside that block. The lock is
    released by an `ExitStack` whether migration succeeds or fails.
  - A restore that starts in that window is refused with "another restore, or an ID'er that is
    starting up, holds this work folder". `recover_before_use()` remains as a thin wrapper.
  - Regression (`test_a_restore_cannot_start_while_idea_serve_is_opening_the_database`): a
    hooked `migrate()` starts a real restore in a child process.
    - The child is refused while startup sits before `migrate()`, with no journal, no staging
      and no database file.
    - It retries and proceeds only once migration has returned.
    - At its first publication it records whether startup's "migration returned" marker
      already existed, and the test requires it did. So the two never have SQLite open at once.
- **P0: a skipped run inside a captured medium is preserved.**
  - For each skipped run of a captured medium, restore preserves `fuse/runs/<run_id>` and every
    bundle that run owns. Ownership comes from the skip record's `bundles`, or from the
    bundle's own `manifest.json` `run_id`.
  - A subtree is preserved only if the snapshot holds nothing under it. Run and bundle names
    must be single, safe path components.
  - Each preserved subtree is reported in the result and in the journal under `preserved`, as
    "preserved: not in this snapshot (run <id>: <reason>)". Whole uncaptured media are
    preserved as before.
  - Regression (`test_a_restore_preserves_a_skipped_run_inside_a_captured_medium`): a `running`
    run with a sealed `fuse/runs` tree and an owned bundle is skipped while its medium is
    captured.
    - Both subtrees change after the backup. After the restore they are byte-identical, and the
      medium's captured journal is restored.
    - Both keys appear in the result and in the publishing journal.
- **Mutation checks:** moving `migrate()` outside the exclusion, and dropping run preservation,
  each make their regression fail.
- **Results (foreground):**
  - `tests/idea_web`: 302 passed. Gates: 27 (ops) and 49 (backup) passed.
  - Ruff check and format are clean, and `git diff --check` is clean.
  - Collection is 1803/1900. Only `backup.py`, `jobs/local.py` and their tests changed, so the
    full shards were not re-run.
