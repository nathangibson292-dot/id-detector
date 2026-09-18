# Build review — 4c-i + 4c-ii: users, sessions, passwords, CSRF; admin-created accounts, admin reset

Base: `f913e91` (worktree fast-forwarded from `9bfacf8`). Not committed (per the brief).
`src/id_detector/` diff: **0 lines**.

## Summary

Hosted mode now has real accounts behind `jobs.user_id`: argon2id passwords, server-side sessions
with a `__Host-` cookie and only `sha256(token)` stored, rotation on sign-in and on plan change,
30-day idle and 90-day absolute expiry against durable columns, server-side logout, single-use
hashed reset and setup links issued by an admin, synchroniser-token CSRF on every mutation plus
the Host/Origin guard, per-IP rate limits with IPv6 /64 buckets and trusted-proxy handling, and an
admin page. There is no self-signup. Local mode is untouched: no cookie, no redirect, no sign-in
page, `idea.cmd` and `idea serve` unchanged, and a test proves it.

This build was paused by the owner part-way through its full-suite run and finished in a second
session. Nothing was redesigned in the second session: it re-ran the shard whose one failing test
(a migration count) had already been fixed, ran the 79 files that had not yet been run, ran the
scripted checks and both gates, and wrote the sections below. No code changed after the pause.

## Changes, file by file

- **`pyproject.toml`, `uv.lock`**: adds `argon2-cffi>=25.1.0`, the planned dependency. It brings in
  `argon2-cffi-bindings`, `cffi` and `pycparser`. `uv sync` was run. No other dependency was
  added.
- **`src/idea_web/migrations/0006_accounts.up.sql`** (new) adds six things:
  - `users`: an opaque `u<hex>` id, the normalised email (unique), an argon2 `pw_hash` (NULL until
    the owner sets it), `role` (`user`/`admin`), `verified_at`, `created_ip`, `created_by`,
    `created_at`, `password_changed_at`, `disabled_at` and `deleted_at`.
  - `sessions`: `token_hash` (sha256, CHECK length 64), `created_at`, `last_seen_at`,
    `expires_at` (absolute), `rotate_required`, `revoked_at`/`revoked_reason` and `replaced_by`.
    A trigger stops a revoked session from being reinstated.
  - `email_tokens`: the §4.5 shape (hash, purpose `setup|reset`, `issued_by`, expiry, `used_at`)
    plus `revoked_at`. A partial unique index allows one outstanding link per account, and a
    trigger makes a used or revoked link immutable.
  - `entitlements`: the §4.5 shape, source `admin` only, with one active plan per account; no
    row means Free.
  - `rate_limit_hits`: per-IP sliding windows.
  - `jobs.user_id` (0005) is untouched: it stays plain TEXT with no foreign key. Old opaque ids
    keep working, and new hosted jobs carry `users.id`.
- **`src/idea_web/migrations/0006_accounts.down.sql`** (new): refuses to run while `users`,
  `entitlements`, `sessions` or `email_tokens` hold rows (the same temp-trigger guard 0003 and
  0005 use). Otherwise it drops the new tables; rate-limit rows are disposable. It runs through
  the unchanged `Database.migrate()`. `backup.py` is **not** changed: backups copy the whole
  database with `sqlite3.backup()`, so the new tables are captured already, and the money
  authority tables are not touched.
- **`src/idea_web/auth.py`** (new), the security core:
  - **Passwords.** argon2id with the parameters stated in code (t=3, m=64 MiB, p=4, 32-byte
    tag, 16-byte salt). The hash is upgraded on sign-in if the parameters change. Passwords are
    NFC-normalised (fix pass 2; NFKC until then), 12 to 1024 characters after normalisation,
    never truncated. No password or raw token is ever logged, echoed or stored.
  - **Timing.** A dummy verify makes a missing account, an account with no password yet, a
    disabled account and a wrong password cost the same argon2 work, and all four get the same
    answer.
  - **Tokens.** 256-bit, with only `sha256` stored.
  - **CSRF.** A session's token is `HMAC(raw session token)`, so it is never stored. Forms shown
    before sign-in use an `HMAC(per-process key, pre-session cookie)` token.
  - **Cookies.** `__Host-idea_session; Path=/; HttpOnly; Secure; SameSite=Lax`. The pre-session
    cookie is `__Host-idea_presession` with `SameSite=Strict`.
  - **`HostedSettings`**: the database, the `https` public origin (normalised) and the trusted
    proxy networks.
  - **Client address.** `client_address()` reads `X-Forwarded-For` only from a trusted peer, and
    walks it from the right.
  - **Limit buckets.** `rate_bucket()` keys an IPv4 address by itself, an IPv6 address by its
    /64, and maps an IPv4-mapped address to IPv4.
  - **`RateLimiter`**: a durable `BEGIN IMMEDIATE` window per action. Login is 10 per 15 min,
    reset 3 per hour and analyse 5 per hour.
  - **`AccountStore`** does everything else, one transaction per operation:
    - `create_account` makes the account pre-verified with no password, issues a setup link and
      writes an audit row.
    - `issue_reset` issues a new link and revokes older ones.
    - `set_plan` changes the entitlement, marks every live session of the account for rotation,
      and writes an audit row.
    - `verify_login` checks a password.
    - `start_session` rotates at sign-in: the session the browser held is revoked.
    - `authenticate` enforces the 30-day idle and 90-day absolute expiry against durable
      columns. It touches `last_seen_at` at most every 5 minutes. When a session is marked for
      rotation, it rotates it with a compare-and-set, and the new session keeps the original
      absolute expiry.
    - `revoke_session` is the server-side logout.
    - `redeem_link` hashes the password outside the transaction. Inside it, it claims the link
      with a compare-and-set, sets the password, and revokes every session and outstanding link
      of the account.
- **`src/idea_web/hosted.py`** (new), the hosted routes:
  - `GET/POST /login`: the limit applies before any lookup, and every failure gives one generic
    message with a 401.
  - `POST /logout`: needs the CSRF token; it revokes the session on the server and clears the
    cookie.
  - `GET/POST /reset/<token>`: the GET neither reads nor writes the database. The POST checks
    the password first (a rejected password is not counted), then applies the reset limit, then
    redeems the link and signs the owner in.
  - `GET /admin`, `POST /admin/users`, `POST /admin/users/<id>/reset` and
    `POST /admin/users/<id>/plan`: each needs an admin session and the CSRF token, and each
    single-use link is shown once, to the admin who issued it.
  - `finish()` sets a rotated session's new cookie, or clears a cookie that no longer works.
- **`src/idea_web/admin_cli.py`** (new): `python -m idea_web.admin_cli create|reset --database
  --origin --email [--admin]`. This is how the operator creates the first admin, since there is
  no sign-up. It prints the link once.
- **`src/idea_web/application.py`**:
  - `create_app(..., hosted=HostedSettings)`: local mode refuses `hosted`, and hosted mode
    requires it (and migrates its database).
  - A `_Viewer` (the CSRF token to check, the one to render, and the session) is threaded
    through every page and mutation. Local mode's viewer is the unchanged process-wide token.
  - `_hosted_get` and `_hosted_post` add the rules below. The local routing branch is the
    previous code with `viewer` passed through.
    - Only `/healthz`, `/static/*`, `/login` and `/reset/*` are public. Anything else needs a
      live session: without one, a page request is sent to `/login` (303) and a script request
      gets a 401.
    - `/admin` gives a 404 to anyone who is not an admin.
    - `/analyse` records the account as the job's `user_id` and applies the analyse limit just
      before the job is queued.
    - `/library/remove` is admin-only on a hosted server until results have per-account owners
      (4d).
  - The account strip (email, Admin link, Sign out) is added to hosted pages only.
- **`src/idea_web/http.py`**:
  - New `hosted_cross_site` and `HostedRequestGuard`:
    - every request must name the public host (the `/healthz` probe is exempt);
    - every POST must carry exactly the public `Origin`, and a missing one is refused.
  - `install_middleware(app, hosted=None)` puts that guard where `LoopbackPostGuard` sits. With
    no argument (local mode, `idea truth review`) the stack is exactly as before. The 4a-ii and
    4a-iii Host/Origin and CSRF protections, the per-document CSP, same-origin framing both ways
    and `SAMEORIGIN` are unchanged, and the new pages go through the same header path.
- **`src/idea_web/pages.py`**: adds `ACCOUNT_CSS` as a separate hosted-only asset
  (`HOSTED_CSS` / `HOSTED_STYLESHEET_HREF`, served only by a hosted server and linked only from
  hosted pages via `head_html(hosted=True)`), `brand_bar_html()` and `account_strip_html()`.
  The local `STATIC_CSS`, `STATIC_JS` and `ASSET_VERSION` (`7ece3e97c2ae`) are byte-for-byte
  what main has (fix pass, item 7).
- **`src/idea_web/templates/`**: new `login.html`, `set_password.html` and `admin.html`. They
  are autoescaped and have no inline script. `README.md` notes them.
- **Tests adjusted:**
  - `test_parity.py::test_hosted_mode_suppresses_audio_urls_and_audio_routes` now makes its
    hosted checks through a signed-in browser, because hosted mode now requires accounts.
  - The schema-version pins go from 5 to 6 in `test_worker.py`, `test_coalescing.py` and
    `test_followup_round2/6/7/8/9.py`. `round9`'s applied-migrations list gains `(6, 1)`.

## Tests added

- `tests/idea_web/hosted_helpers.py`: a cookie-keeping in-process `Browser` on
  `https://idea.test`, a settable `Clock`, `RecordingJobs`, `make_account` and
  `database_text`.
- `tests/idea_web/test_auth.py`, the 4c-i gate (19 tests, 25 after the fix pass below):
  - passwords: argon2 parameters and the upgrade on sign-in;
  - the cookie's attributes, with only its hash stored and nothing logged;
  - every page needs a session;
  - a failed sign-in gives the same answer and the same argon2 work whatever the reason;
  - rotation at sign-in; logout on the server;
  - idle and absolute expiry, including after a restart;
  - rotation of every session on a plan change, plus a 2-thread race that rotates exactly
    once;
  - CSRF on every mutation, and pre-session CSRF;
  - Host and Origin checks;
  - the per-document CSP and same-origin framing;
  - the login limit with IPv6 /64 buckets; the reset and analyse limits;
  - trusted-proxy handling;
  - a hosted job carries `users.id`, and legacy ids still work;
  - the 0006 down-migration guard;
  - **local mode stays sign-in free**: see "How local mode stays sign-in free" below.
- `tests/idea_web/test_accounts.py`, the 4c-ii gate (8 tests, 10 after the fix pass below):
  - **an admin-created account is pre-verified**, and its setup link is single-use;
  - the operator CLI bootstrap;
  - **an admin reset link works exactly once**, the database trigger enforces it, and every
    old session ends;
  - expiry, and a newer link replacing an older one;
  - **a real race**: two threads, 3 rounds, then two spawned processes. Exactly one password
    is set each time;
  - **XSS through comments and titles is escaped**: admin comments, mix titles, and the admin,
    analyse, sign-in and password error pages;
  - **there is no self-signup route**: 12 candidate paths, anonymous and signed in, plus a
    check that the app registers only its catch-all routes;
  - admin gating, audit rows, the one-active-plan rule, and no admin surface in local mode.

All tests are offline and deterministic: the clock is injectable, the races are real threads and
real spawned processes on one SQLite file, and no provider is called.

## How local mode stays sign-in free

`create_app(local=True)` never builds `HostedAuth`, refuses `hosted=` settings, and installs the
unchanged `LoopbackPostGuard` stack. Every local route uses the one process-wide token, exactly
as before. `/login`, `/logout`, `/admin*`, `/reset/*` and `/signup` all give a 404 there. No local
response sets a cookie or redirects to a sign-in page. `idea serve` and `idea.cmd` are unchanged:
`server.make_server` still calls `create_app(local=True)`. The local queue database migrates to 6
like every migration, and its account tables stay empty.

`test_local_mode_stays_sign_in_free_and_unchanged` proves all of this in-process. It also checks
the real loopback server `serve_in_background` on port 0 (not 8791/8792): plain HTTP, no cookie,
submitting works, and `/admin` gives a 404.

## Reversion demonstration

Each safeguard was reverted in turn, both gates were run, and the file was restored and checked
by sha256. Every reversion failed the gates:

```text
R1 cookie not Secure: 1 failed (sign-in cookie test)
R2 link used without checking it is unused (lookup, claim and trigger): 4 failed (exactly-once, race, expiry, CLI)
R3 no dummy verify for a missing account: 1 failed (login-failure test)
R4 plan change does not rotate: 2 failed (plan-change rotation, rotation race)
R5 sign-in keeps the held session: 1 failed (sign-in rotation)
R6 logout only clears the cookie: 1 failed (server-side logout)
R7 no idle expiry: 1 failed (idle/absolute expiry)
R8 no autoescape: 4 failed (XSS test and the link pages)
R9 admin surface open to any session: 1 failed (no self-signup)
R10 IPv6 bucketed per address: 2 failed (login limit, proxy test)
R11 X-Forwarded-For believed from anyone: 1 failed (proxy test)
R12 missing Origin accepted: 1 failed (Host/Origin test)
R13 local mode accepts accounts: 1 failed (local-mode test)
R14 single-use trigger dropped: 1 failed (exactly-once)
R15 rotation extends the absolute lifetime: 1 failed (plan-change rotation)
```

## Plan ambiguities resolved

1. **Plan change needs a plan.** §4.4 says sessions rotate on a plan change, but credits and
   tenancy are 4d. A minimal admin-only `entitlements` table and a Set plan action were added so
   that rotation on a plan change is real and tested. Credits and tenancy are untouched.
2. **The first admin** comes from `admin_cli`, because there is no sign-up (D-decisions in §6.1).
3. **Links.** M1 has no email provider, so a setup or reset link is shown once to the issuing
   admin (or printed once by the CLI). The token itself is never stored.
4. **The reset limit (3 per hour)** counts attempts to use a link. A rejected password does not
   count, so a typo cannot lock an owner out of their own reset.
5. **Hosted `/library/remove`** is admin-only until per-account ownership arrives (4d), because a
   shared library with no owners would otherwise let any account remove any result.
6. **Tenancy is 4d.** Job pages and the library are not yet scoped per user, and hosted still
   cannot start in production without the 6a hosted-ready gate.
7. **Old sessions.** Revoked and expired sessions are kept, with no clean-up job. They are small
   and inert (the reinstate trigger stops a revoked row coming back).
8. **`jobs.user_id` keeps no foreign key**, so the 4b-iv rows with opaque ids stay valid and the
   0006 down-migration does not need to touch `jobs`.

## Pasted outputs

Every run was in the foreground, in the worktree, with no server on 8791/8792, no PowerShell
gate, no live provider call and nothing written to `data/` or `work/`. Warnings lines (the
`pydub`/`audioop` deprecation) are elided.

### Collected total

```text
$ uv run pytest --collect-only -q
1862/1959 tests collected (97 deselected) in 7.56s
# 98 collected files: 19 under tests/idea_web/, 79 under tests/
```

### Full suite in shards

The shard globs partition every `tests/idea_web/test_*.py` and `tests/test_*.py` file, so each
collected file ran exactly once. Shard C ran close to the runner's 10-minute cap, so the rest of
`tests/` was split into four smaller shards. The `deselected` counts are the `slow`/`live` tests
that the default `-m 'not slow and not live'` always deselects (25 + 68 + 4 = 97).

```text
Shard A: tests/idea_web/test_backup.py test_coalescing.py test_followup_*.py  (11 files)
173 passed, 1 warning in 205.02s (0:03:25)

Shard B: tests/idea_web/test_accounts.py test_auth.py test_headers_forms.py test_legacy_contract.py
         test_local_queue.py test_ops.py test_parity.py test_worker.py  (8 files)
188 passed, 1 warning in 84.63s (0:01:24)

Shard C: tests/test_[a-p]*.py  (31 files)
602 passed, 1 warning in 538.31s (0:08:58)

Shard D1: tests/test_s[a-e]*.py  (5 files)
127 passed, 1 warning in 99.44s (0:01:39)

Shard D2: tests/test_stage[1-3]*.py
189 passed, 25 deselected, 1 warning in 39.97s

Shard D3: tests/test_stage[4-9]*.py
278 passed, 1 skipped, 68 deselected, 1 warning in 23.02s

Shard E: tests/test_t*.py  (12 files)
303 passed, 1 skipped, 4 deselected, 1 warning in 235.34s (0:03:55)
```

Total: **1860 passed + 2 skipped = 1862**, equal to the collected total. **0 failed.** The
known timing-sensitive backup test
(`test_a_restore_killed_mid_publication_is_recovered_when_idea_serve_starts`) passed in shard A
first time. Shard A's earlier single failure (`round9`, a missing `(6, 1)` in its migration list)
was fixed before the pause and the whole shard is green on re-run.

### Ruff

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
386 files already formatted
```

### Scripts

```text
$ uv run python scripts/audit_fixtures.py
audited 517 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

### Both gates, exactly

```text
$ uv run pytest tests/idea_web/test_auth.py -q
...................                                                      [100%]
19 passed, 1 warning in 27.73s

$ uv run pytest tests/idea_web/test_accounts.py -q
........                                                                 [100%]
8 passed, 1 warning in 15.91s
```

### Git

```text
$ git diff --stat
 pyproject.toml                         |   1 +
 src/idea_web/application.py            | 284 +++++++++++++++++++++++++++------
 src/idea_web/http.py                   |  49 +++++-
 src/idea_web/pages.py                  |  46 +++++-
 src/idea_web/templates/README.md       |   5 +
 tests/idea_web/test_coalescing.py      |   6 +-
 tests/idea_web/test_followup_round2.py |   2 +-
 tests/idea_web/test_followup_round6.py |   4 +-
 tests/idea_web/test_followup_round7.py |   2 +-
 tests/idea_web/test_followup_round8.py |   8 +-
 tests/idea_web/test_followup_round9.py |  12 +-
 tests/idea_web/test_parity.py          |  13 +-
 tests/idea_web/test_worker.py          |   6 +-
 uv.lock                                |  67 ++++++++
 14 files changed, 428 insertions(+), 77 deletions(-)

$ git status --short
 M pyproject.toml
 M src/idea_web/application.py
 M src/idea_web/http.py
 M src/idea_web/pages.py
 M src/idea_web/templates/README.md
 M tests/idea_web/test_coalescing.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_parity.py
 M tests/idea_web/test_worker.py
 M uv.lock
?? docs/reviews/build-4c.md
?? src/idea_web/admin_cli.py
?? src/idea_web/auth.py
?? src/idea_web/hosted.py
?? src/idea_web/migrations/0006_accounts.down.sql
?? src/idea_web/migrations/0006_accounts.up.sql
?? src/idea_web/templates/admin.html
?? src/idea_web/templates/login.html
?? src/idea_web/templates/set_password.html
?? tests/idea_web/hosted_helpers.py
?? tests/idea_web/test_accounts.py
?? tests/idea_web/test_auth.py

$ git diff -- src/id_detector | wc -l
0
```

New (untracked) files: `auth.py` 757 lines, `hosted.py` 404, `admin_cli.py` 77, the two 0006
migrations 114 + 28, the three templates 39 + 15 + 13, `hosted_helpers.py` 182,
`test_accounts.py` 528, `test_auth.py` 787.

Protected and other-session files are untouched: `docs/PLAN-v2.md`, `profiles/`, `data/`,
`work/`, `src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd` and
`src/id_detector/present/theme.py` do not appear in `git status`.

## What was not done, and what I could not do

- **Not committed, no branch** (per the brief). The reviewer commits.
- **The PowerShell gates were not run** and no server was started on 8791/8792 (per the brief);
  the orchestrator runs those. The local-mode proof is the in-process test plus a real loopback
  server on port 0.
- **No email delivery.** Links are shown once to the issuing admin or printed by the CLI. An
  email provider is not in this cycle's plan.
- **No tenancy or credits.** Job pages and the library are not scoped per account, so hosted
  `/library/remove` is admin-only for now. That is 4d.
- **No session clean-up job.** Revoked and expired rows are retained.
- **No self-service password change** while signed in. §4.4 does not ask for one in M1; the
  admin reset covers it.
- **`docs/STATUS.md` was not updated.** The brief asks only for this report; the reviewer or
  the orchestrator can move the status line when the cycle is merged.
- **Shard timing.** `tests/test_[a-p]*.py` took 538 s, just under the 600 s per-command cap of
  this runner. If the reviewer re-runs, the four-way split of the rest of `tests/` above is the
  safer shape; every shard is independent.

## Fix pass (review `diff-review-4c-sol`: FIX_FIRST)

Same worktree, still uncommitted, `src/id_detector/` still **0 lines**. Every item below has a
regression that fails when its fix is reverted (the reversion table follows the items).

### The eight items, and what changed

1. **P0 — a session is never issued from a credential that has since changed. DONE.**
   `users.pw_version` (0006, `INTEGER NOT NULL DEFAULT 0`) is the credential's version;
   `redeem_link` bumps it (`pw_version = pw_version + 1`) in the transaction that sets the
   password, revokes every session of the account and revokes every outstanding link, and
   returns the `Account` read *inside* that transaction, carrying the new version.
   `Account.credential` is the version a `verify_login` saw. `start_session(user_id, *,
   credential, ...)` now inserts with `INSERT ... SELECT ... FROM users WHERE id = ? AND
   pw_version = ? AND pw_hash IS NOT NULL AND deleted_at IS NULL AND disabled_at IS NULL` and
   returns `None` when no row was inserted: one write transaction, no read-then-write gap. The
   sign-in and set-password routes answer `None` with the same 401 `LOGIN_FAILED` (or 400
   `LINK_FAILED`) as any other failure and set no cookie. Regression
   (`test_accounts.py::test_a_sign_in_that_verified_the_old_password_loses_to_a_reset_that_completes_first`)
   is barrier-ordered exactly as asked: the app's `verify_login` is wrapped to verify the old
   password and then hold on an event; the owner's reset completes over HTTP; the hold is
   released; the sign-in must be a 401 with no cookie, the only live session must be the one the
   reset started, and at store level `start_session(credential=<old>)` is `None` while
   `start_session(credential=<new>)` succeeds.
2. **P1 — export limit. DONE.** `RATE_LIMITS["export"] = (10, 60.0)`. On a hosted server a
   signed-in GET of a result's `.cue`, `.md` or `.json` (`_is_export`, the shape `_result_file`
   serves) is admitted through the per-IP limiter before `_get`; a refusal is a 429 with
   `Retry-After`. The result page and the live pages are not exports. Regression:
   `test_auth.py::test_exports_are_limited_to_ten_per_minute_per_ip` (a real published result
   from the 1a bundle fixtures; 10 admitted, the 11th refused, another address unaffected, the
   window slides, an anonymous request is sent to sign in and not counted).
3. **P1 — hosted `/analyse` uses the real durable queue. DONE.** No second queue. The EXISTING
   adapter `LocalJobs` gained `submit(..., user_id=None)`, passed straight to
   `JobQueue.enqueue(user_id=)` (4b-iv), and a `local_mode` flag (default `True`) handed to the
   queue, so a hosted adapter refuses a local file target. `JobQueueAdapter.submit` declares
   `user_id`. Regression:
   `test_auth.py::test_a_hosted_submission_goes_through_the_durable_queue_as_the_account` runs
   the hosted app over `LocalJobs(work, database=settings.database, local_mode=False)`: form and
   JSON submissions land in `jobs` with `user_id = <account>` and state `intake`; the job page,
   status poll, home activity, cancel and dismiss read that row; a server file is refused (400);
   both the protocol and the adapter name `user_id`.
4. **P1 — cookie rotation race. DONE.** `AccountStore.rotated_away(token)` says whether a
   cookie's session ended by rotation (`revoked_reason = 'rotated'` with `replaced_by` set).
   `HostedAuth.finish()` no longer clears such a cookie: the request that rotated it set the
   replacement on the same browser, and the two answers can arrive in either order. Every other
   dead cookie is still cleared (the logout case is asserted). Regression:
   `test_auth.py::test_a_stale_answer_never_clears_a_freshly_rotated_cookie`, HTTP level: the
   rotating answer and the stale answer are applied to a cookie jar in both orders and the jar
   must hold the replacement; then a real 2-thread barrier race, three rounds, asserting exactly
   one answer rotates and the other sets no cookie at all.
5. **P1 — migrations under the supervisor lock. DONE.** `Database(path, supervisor_lock=...)`
   names the lock for a database that is not a work root's `.idea/app.db` (whose lock is
   implied as before). `idea_web.database.hosted_database(path)` opens a hosted database with
   that lock (`worker-supervisor.lock` next to the file, or the work root's own lock) and
   migrates under it: `Database.migrate` takes it before any DDL and refuses with
   `MigrationRefused` when it is held, exactly the `local_database()` path. Hosted
   `create_app` refuses a database with no supervisor lock (`ValueError`), and the admin CLI
   opens through `hosted_database` and reports a refusal as exit 2. The hosted
   `Worker.run_forever(supervisor_lock=...)` holds that lock for its whole run, so no migration
   can run under a live worker and a second worker on the lock is refused. Regression:
   `test_auth.py::test_hosted_migrations_take_the_worker_supervisor_lock` (lock held:
   `hosted_database`, the CLI and `create_app` all refuse and nothing is applied; released: all
   work; unsupervised database: `ValueError`; a live `Worker` thread: `migrate(5)` is refused
   and a second worker gets `JobStoreLocked`; stopped: the migration runs).
6. **P1 — password length after normalisation, never truncated. DONE.** `verify_login`
   normalises first (NFKC, the form the hash was made from and the form `password_post` already
   checks; kept NFKC rather than NFC so sign-in and reset agree with the stored hash), applies
   the 1024 bound to that, and passes the whole normalised string to argon2. Regression:
   `test_accounts.py::test_unicode_passwords_are_bounded_after_normalisation_and_never_truncated`:
   1,100 typed code points (`e` + combining acute) compose to 550, accepted by the reset and
   signed in typed either way; the 1,024-code-point prefix is refused (nothing truncated); 100
   code points that expand to 1,800 are refused the same way when chosen and when tried.
7. **P2 — assets and imports. DONE.** `STATIC_CSS` is `theme.BASE_CSS + legacy._APP_CSS`
   again; `ACCOUNT_CSS` is served as the hosted-only `/static/hosted.<hash>.css` (only by a
   hosted server) and linked only by hosted heads. `application.py` imports `idea_web.auth` /
   `idea_web.hosted` only under `TYPE_CHECKING` and inside `create_app` / the hosted handlers.
   Regression: `test_auth.py::test_local_assets_and_start_up_imports_are_untouched_by_accounts`
   (bytes equal to the pre-accounts composition, local 404 for the hosted asset, hosted pages
   link and serve it, and a subprocess importing `idea_web.application`, `idea_web.server` and
   `idea_web.jobs.local` has none of `argon2`, `idea_web.auth`, `idea_web.hosted` loaded).
   Checked against main: `ASSET_VERSION` `7ece3e97c2ae` on both, `STATIC_CSS`/`STATIC_JS`
   byte-equal.
8. **P2 — access logs. DONE (redaction, proven).** `idea_web.hosted.SecretPathFilter` rewrites
   `/reset/<43-char token>` to `/reset/[redacted]` in a record's message and arguments;
   `redact_secret_paths()` installs it on `uvicorn.access`, `uvicorn.error` and `uvicorn`, and
   hosted `create_app` calls it. Regression:
   `test_auth.py::test_hosted_access_logs_never_carry_a_setup_or_reset_token` runs a REAL
   uvicorn server (127.0.0.1, port 0, `access_log=True`, `log_level="info"`) over the hosted
   app, fetches `/reset/<token>`, and asserts the access log names the request with
   `/reset/[redacted]`, the token appears in no record, and the log is on (`/healthz` logged).

Other edits in the pass: `tests/idea_web/hosted_helpers.py` opens its database through
`hosted_database`; the CLI test opens its `create_app` database the same way; the store-level
rotation race passes `credential=`. `Account` gained `credential`; `_ACCOUNT_COLUMNS` reads
`pw_version`.

### Reversion demonstration (fix pass)

Each safeguard was reverted with a one-line patch, its regression run, and the file restored and
verified by sha256 (`scratchpad/revert_fix_demo.py`). Every reversion failed its regression:

```text
R1 session inserted whatever the credential version (P0): FAILED as required -- 1 failed
R2 no export limit: FAILED as required -- 1 failed
R3 durable adapter drops the account: FAILED as required -- 1 failed
R4 a stale answer clears the cookie: FAILED as required -- 1 failed
R5a hosted_database names no supervisor lock: FAILED as required -- 1 failed
R5b the hosted worker does not hold the lock: FAILED as required -- 1 failed
R5c hosted startup accepts an unsupervised database: FAILED as required -- 1 failed
R6 the typed password is bounded and truncated before normalisation: FAILED as required -- 1 failed
R7a hosted CSS back in the local stylesheet: FAILED as required -- 1 failed
R7b account code imported eagerly by the local application: FAILED as required -- 1 failed
R8 no access-log redaction: FAILED as required -- 1 failed
restored and verified: application.py, auth.py, database.py, hosted.py, jobs/local.py, jobs/worker.py, pages.py
```

R1 is the P0: with `pw_version = ?` made vacuous (`(pw_version = ? OR 1)`, same bindings), the
held sign-in gets a session after the reset and the regression fails.

### Pasted outputs (fix pass)

All foreground, in the worktree; no server on 8791/8792, no PowerShell gate, no live provider,
nothing written to `data/` or `work/`. Shard C was slowed past the runner's 10-minute cap by
running next to shard B and finished in the background; it was then waited for in the
foreground and its result read (602 passed, 782 s). Every other shard ran one after another.

```text
$ uv run pytest --collect-only -q
1870/1967 tests collected (97 deselected)      # 1862 before the pass + 8 new regressions

Shard A: tests/idea_web/test_backup.py test_coalescing.py test_followup_*.py
173 passed, 1 warning in 344.78s (0:05:44)
Shard B: tests/idea_web/test_accounts.py test_auth.py test_headers_forms.py test_legacy_contract.py
         test_local_queue.py test_ops.py test_parity.py test_worker.py
196 passed, 1 warning in 157.47s (0:02:37)
Shard C: tests/test_[a-p]*.py
602 passed, 1 warning in 782.29s (0:13:02)
Shard D1: tests/test_s[a-e]*.py
127 passed, 1 warning in 185.64s (0:03:05)
Shard D2: tests/test_stage[1-3]*.py
189 passed, 25 deselected, 1 warning in 73.57s (0:01:13)
Shard D3: tests/test_stage[4-9]*.py
278 passed, 1 skipped, 68 deselected, 1 warning in 53.20s
Shard E: tests/test_t*.py
303 passed, 1 skipped, 4 deselected, 1 warning in 269.19s (0:04:29)
```

Total: **1868 passed + 2 skipped = 1870**, equal to the collected total; **0 failed**. The
timing-sensitive backup test passed first time.

```text
$ uv run pytest tests/idea_web/test_auth.py -q
.........................                                                [100%]
25 passed, 1 warning in 46.61s

$ uv run pytest tests/idea_web/test_accounts.py -q
..........                                                               [100%]
10 passed, 1 warning in 26.16s

$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
386 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 518 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js

$ git diff --stat
 pyproject.toml                         |   1 +
 src/idea_web/application.py            | 333 +++++++++++++++++++++++++++------
 src/idea_web/database.py               |  43 ++++-
 src/idea_web/http.py                   |  49 ++++-
 src/idea_web/jobs/local.py             |  12 +-
 src/idea_web/jobs/worker.py            |  43 +++--
 src/idea_web/pages.py                  |  61 +++++-
 src/idea_web/templates/README.md       |   5 +
 tests/idea_web/test_coalescing.py      |   6 +-
 tests/idea_web/test_followup_round2.py |   2 +-
 tests/idea_web/test_followup_round6.py |   4 +-
 tests/idea_web/test_followup_round7.py |   2 +-
 tests/idea_web/test_followup_round8.py |   8 +-
 tests/idea_web/test_followup_round9.py |  12 +-
 tests/idea_web/test_parity.py          |  13 +-
 tests/idea_web/test_worker.py          |   6 +-
 uv.lock                                |  67 +++++++
 17 files changed, 564 insertions(+), 103 deletions(-)

$ git status --short
 M pyproject.toml
 M src/idea_web/application.py
 M src/idea_web/database.py
 M src/idea_web/http.py
 M src/idea_web/jobs/local.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/pages.py
 M src/idea_web/templates/README.md
 M tests/idea_web/test_coalescing.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_parity.py
 M tests/idea_web/test_worker.py
 M uv.lock
?? docs/reviews/build-4c.md
?? src/idea_web/admin_cli.py
?? src/idea_web/auth.py
?? src/idea_web/hosted.py
?? src/idea_web/migrations/0006_accounts.down.sql
?? src/idea_web/migrations/0006_accounts.up.sql
?? src/idea_web/templates/admin.html
?? src/idea_web/templates/login.html
?? src/idea_web/templates/set_password.html
?? tests/idea_web/hosted_helpers.py
?? tests/idea_web/test_accounts.py
?? tests/idea_web/test_auth.py

$ git diff -- src/id_detector | wc -l
0
```

Base is still `f913e91`; no commit, no new branch. Protected and other-session paths are
untouched. Local mode remains sign-in free (`test_local_mode_stays_sign_in_free_and_unchanged`
passed in shard B, and item 7 proves local start-up loads no account code).

### Still not done (unchanged from above)

No email delivery, no tenancy or credits (4d), no session clean-up job, no self-service
password change, `docs/STATUS.md` untouched, PowerShell gates not run. The hosted worker
launcher (6a) must pass `supervisor_lock=` to `Worker.run_forever` and open its database with
`hosted_database`, as the tests here do. (Superseded by fix pass 2: the worker now takes its
database's lock by itself.)

## Fix pass 2 (review `diff-review-4c-sol-r2`: FIX_FIRST on three P1s)

Same worktree, still uncommitted, `src/id_detector/` still **0 lines**. Items 1, 2, 3, 7 and 8
of the first pass are untouched.

### The three items

1. **P1 — cookie replacement on every path. DONE.** The rule is now "a browser that was handed
   its replacement never has that cookie cleared", on every path that hands one out:
   - rotation on plan change already stamped `replaced_by`;
   - `start_session(replacing=<the cookie the browser sent>)` — the sign-in and the
     set-password paths — now revokes the old session if it is still live (`replaced`) AND
     stamps `replaced_by = <new session id>` on it even when the reset that led here has
     already revoked it (`password`), in the same write transaction as the insert;
   - `AccountStore.rotated_away` became `superseded(token)`: `replaced_by IS NOT NULL`,
     whatever the revocation reason; `HostedAuth.finish()` clears only a cookie that is not
     superseded. Logout, expiry and a reset made from another browser still clear.
   Regression: `test_auth.py::test_a_stale_answer_never_clears_a_freshly_issued_cookie`,
   parametrised over `plan`, `login` and `reset`, HTTP level. For each path: ordered (the
   superseding answer, then an in-flight answer for the old cookie: the latter sets no cookie,
   and a jar fed both answers in either order holds the replacement), then two barrier-released
   races of the superseding request against a request with the cookie it supersedes (exactly
   one answer carries a cookie, it is never a clearing one, and both orders keep it); finally
   the negative cases (a logged-out cookie and one revoked by a reset from another browser are
   still cleared). `test_sign_in_rotates_the_session_and_the_old_token_stops_working` was
   adjusted for the login path: the copy's answer is a 303 with no `Set-Cookie` (before, a
   clearing cookie), because that copy is the browser the sign-in just replaced.
2. **P1 — a hosted worker never runs unsupervised. DONE.** `Worker.run_forever()` takes the
   lock its database names (`database.supervisor_lock_path`) by itself; `supervisor_lock=` is
   now only an override. A database that names no lock is refused before anything is claimed
   with the new `UnsupervisedWorker` (pointing at `hosted_database`). The regression
   (`test_auth.py::test_hosted_migrations_take_the_worker_supervisor_lock`) starts the worker
   with NO lock argument: it holds `<database dir>/worker-supervisor.lock`, `migrate(5)` is
   refused under it, a second lock-less worker gets `JobStoreLocked`, the lock is released with
   the run, and `Worker(<unsupervised database>).run_forever()` raises `UnsupervisedWorker`.
   The three existing `run_forever` callers in the tests are `LocalWorker` (already under the
   `idea serve` supervisor), so nothing else changed.
3. **P1 — NFC, not NFKC. DONE.** `normalise_password` is NFC, and it is the one function every
   path uses: setup and reset (`password_post` normalises both fields before `password_problem`
   compares and bounds them), hashing (`hash_password`), sign-in (`verify_login` normalises,
   bounds the normalised form, never truncates) — so compatibility characters are distinct
   credentials again while canonically equivalent spellings stay one password. Regression:
   `test_accounts.py::test_compatibility_distinct_passwords_are_distinct_credentials` — a
   password with `ﬀ`, `①`, `Ⅳ` and a full-width `a` and its ASCII look-alike are NFKC-equal
   but do not authenticate each other in either direction (chosen via the link, tried at
   sign-in and at store level), while `café Å` chosen precomposed and confirmed decomposed
   (`e` + combining acute, `Å`) is accepted at the confirmation field and signs in typed
   either way. The earlier "normalised form too long" case used an NFKC-only expansion; it now
   uses a canonical composition exclusion (U+0958 → two code points under NFC): 1,000 typed,
   1,100 normalised, refused when chosen and when tried. Email normalisation stays NFKC
   (an address is an identifier, not a credential).

### Reversion demonstration (fix pass 2)

Same script, same restore-and-verify. Every reversion failed its regression (`-x`: the
parametrised cookie test stops at its first failing path, `[login]`, after `[plan]` passes):

```text
R4b sign-in/set-password do not mark the old session replaced_by (pass 2): FAILED as required -- 1 failed, 1 passed
R4c superseded means rotated only (pass 2): FAILED as required -- 1 failed, 1 passed
R5d the worker runs without acquiring its database's lock (pass 2): FAILED as required -- 1 failed
R5e an unsupervised database is not refused (pass 2): FAILED as required -- 1 failed
R6b NFKC instead of NFC (pass 2): FAILED as required -- 1 failed
restored and verified: src/idea_web/auth.py, src/idea_web/jobs/worker.py
```

### Pasted outputs (fix pass 2)

All foreground, one shard after another, each waited for; no server on 8791/8792, no
PowerShell gate, no live provider, nothing written to `data/` or `work/`. The former shard C
was split so no run approached the runner's cap.

```text
$ uv run pytest --collect-only -q
1873/1970 tests collected (97 deselected)      # 1870 + 1 NFC test + the cookie test's 2 extra parameters

Shard A:   tests/idea_web/test_backup.py test_coalescing.py test_followup_*.py
173 passed, 1 warning in 241.79s (0:04:01)
Shard B:   tests/idea_web/test_accounts.py test_auth.py test_headers_forms.py test_legacy_contract.py
           test_local_queue.py test_ops.py test_parity.py test_worker.py
199 passed, 1 warning in 111.01s (0:01:51)
Shard C1:  tests/test_[a-f]*.py
122 passed, 1 warning in 80.35s (0:01:20)
Shard C2a: tests/test_[g-o]*.py tests/test_p[i-z]*.py
55 passed, 1 warning in 25.98s
Shard C2b: tests/test_p[a-h]*.py
425 passed, 1 warning in 374.16s (0:06:14)
Shard D1:  tests/test_s[a-e]*.py
127 passed, 1 warning in 110.15s (0:01:50)
Shard D2:  tests/test_stage[1-3]*.py
189 passed, 25 deselected, 1 warning in 43.01s
Shard D3:  tests/test_stage[4-9]*.py
278 passed, 1 skipped, 68 deselected, 1 warning in 18.16s
Shard E:   tests/test_t*.py
303 passed, 1 skipped, 4 deselected, 1 warning in 132.61s (0:02:12)
```

Total: **1871 passed + 2 skipped = 1873**, equal to the collected total; **0 failed**. The
timing-sensitive backup test passed first time.

```text
$ uv run pytest tests/idea_web/test_auth.py -q
...........................                                              [100%]
27 passed, 1 warning in 48.19s

$ uv run pytest tests/idea_web/test_accounts.py -q
...........                                                              [100%]
11 passed, 1 warning in 35.96s

$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
386 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 518 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js

$ git diff --stat
 pyproject.toml                         |   1 +
 src/idea_web/application.py            | 333 +++++++++++++++++++++++++++------
 src/idea_web/database.py               |  43 ++++-
 src/idea_web/http.py                   |  49 ++++-
 src/idea_web/jobs/local.py             |  12 +-
 src/idea_web/jobs/worker.py            |  54 ++++--
 src/idea_web/pages.py                  |  61 +++++-
 src/idea_web/templates/README.md       |   5 +
 tests/idea_web/test_coalescing.py      |   6 +-
 tests/idea_web/test_followup_round2.py |   2 +-
 tests/idea_web/test_followup_round6.py |   4 +-
 tests/idea_web/test_followup_round7.py |   2 +-
 tests/idea_web/test_followup_round8.py |   8 +-
 tests/idea_web/test_followup_round9.py |  12 +-
 tests/idea_web/test_parity.py          |  13 +-
 tests/idea_web/test_worker.py          |   6 +-
 uv.lock                                |  67 +++++++
 17 files changed, 575 insertions(+), 103 deletions(-)

$ git status --short
 M pyproject.toml
 M src/idea_web/application.py
 M src/idea_web/database.py
 M src/idea_web/http.py
 M src/idea_web/jobs/local.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/pages.py
 M src/idea_web/templates/README.md
 M tests/idea_web/test_coalescing.py
 M tests/idea_web/test_followup_round2.py
 M tests/idea_web/test_followup_round6.py
 M tests/idea_web/test_followup_round7.py
 M tests/idea_web/test_followup_round8.py
 M tests/idea_web/test_followup_round9.py
 M tests/idea_web/test_parity.py
 M tests/idea_web/test_worker.py
 M uv.lock
?? docs/reviews/build-4c.md
?? src/idea_web/admin_cli.py
?? src/idea_web/auth.py
?? src/idea_web/hosted.py
?? src/idea_web/migrations/0006_accounts.down.sql
?? src/idea_web/migrations/0006_accounts.up.sql
?? src/idea_web/templates/admin.html
?? src/idea_web/templates/login.html
?? src/idea_web/templates/set_password.html
?? tests/idea_web/hosted_helpers.py
?? tests/idea_web/test_accounts.py
?? tests/idea_web/test_auth.py

$ git diff -- src/id_detector | wc -l
0
```

Base is still `f913e91`; no commit, no new branch; protected and other-session paths
untouched; local mode remains sign-in free (its test passed in shard B). Still not done:
unchanged from the list above, minus the worker-launcher note (now automatic).

## Fix pass 3 (review `diff-review-4c-sol-r3`: one P1 and its follow-up)

Same worktree, still uncommitted, `src/id_detector/` still **0 lines**; nothing marked DONE
was touched.

- **P1 — a reset is ONE transaction. DONE.** `AccountStore.redeem_and_start_session(token,
  password, *, ip, replacing)` claims the link, sets the password (bumping `pw_version`),
  revokes every session and link of the account, inserts the replacement session and stamps
  `replaced_by` on the cookie the browser sent, all inside one `BEGIN IMMEDIATE`. The pieces
  are the private `_redeem` (shared with `redeem_link`, which keeps its signature and its
  tests) and `_issue_session` (shared with `start_session`); argon2 still runs before the
  transaction. `password_post` calls the combined method; there is no longer a moment at
  which the old session is revoked but not yet superseded.
  Regression: `test_auth.py::test_a_reset_revokes_and_replaces_in_one_transaction`. The
  app's `Database.write` is wrapped so that, right after the first COMMITTED transaction in
  which the old session is revoked, a request with the old cookie is released and answered
  before the reset request goes on. With one transaction that request already sees
  `replaced_by` and sets no cookie; the replacement answer applied before the stale answer
  (and after it) leaves the new cookie in the jar, the old row reads `password` /
  `replaced_by = <new id>`. Reversion R4d (the issue moved into a second transaction) fails
  it in 3 s: the stale request lands in the gap and clears the cookie.
- **Follow-up — `run_forever(supervisor_lock=)` cannot name a different lock. DONE.** The
  worker always takes `database.supervisor_lock_path`; an override that is not that path is
  refused with `UnsupervisedWorker` before anything is claimed (the same path is accepted).
  Added to `test_hosted_migrations_take_the_worker_supervisor_lock` with an already-set stop
  event, so a worker that wrongly accepted the override returns at once instead of running
  for ever (the first attempt at reversion R5f hung for that reason and was killed; the
  script's `finally` restored the file, verified by sha256, and the demonstration was rerun).

```text
R4d the reset is two transactions again: redeem and commit, then issue (pass 3): FAILED as required -- 1 failed in 3.35s
R5f a mismatched supervisor_lock override is accepted (pass 3): FAILED as required -- 1 failed in 2.44s
restored and verified: src/idea_web/auth.py, src/idea_web/jobs/worker.py
```

Outputs (all foreground, one after another, each waited for; `worker.py` was touched, so the
whole suite ran):

```text
$ uv run pytest --collect-only -q
1874/1971 tests collected (97 deselected)      # 1873 + the one-transaction test

Shard A:   173 passed, 1 warning in 215.75s (0:03:35)
Shard B:   200 passed, 1 warning in 106.89s (0:01:46)      # tests/idea_web = A + B
Shard C1:  122 passed, 1 warning in 79.68s (0:01:19)
Shard C2a: 55 passed, 1 warning in 25.68s
Shard C2b: 425 passed, 1 warning in 361.43s (0:06:01)
Shard D1:  127 passed, 1 warning in 91.97s (0:01:31)
Shard D2:  189 passed, 25 deselected, 1 warning in 36.58s
Shard D3:  278 passed, 1 skipped, 68 deselected, 1 warning in 18.03s
Shard E:   303 passed, 1 skipped, 4 deselected, 1 warning in 124.44s (0:02:04)
Total: 1872 passed + 2 skipped = 1874, equal to the collected total; 0 failed.

$ uv run pytest tests/idea_web/test_auth.py -q
28 passed, 1 warning in 32.79s
$ uv run pytest tests/idea_web/test_accounts.py -q
11 passed, 1 warning in 24.10s
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
386 files already formatted
$ git diff --stat | tail -1
 17 files changed, 581 insertions(+), 103 deletions(-)
$ git diff -- src/id_detector | wc -l
0
```

`git status --short` lists the same 29 entries as fix pass 2 (17 modified, 12 untracked).
No commit, no new branch, nothing written to `data/` or `work/`, no live provider, no
PowerShell gate, no server on 8791/8792; local mode remains sign-in free.
