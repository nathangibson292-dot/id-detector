## A. Contract violations

| Round-2 requirement | Status | Evidence / reversion coverage |
|---|---|---|
| Replacement cookie survives login, reset, and plan-change races | **PARTIAL** | Login replacement is atomic in [`auth.py:661`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:661>), and plan rotation is atomic at [`auth.py:727`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:727>). Reset revocation and replacement are not atomic; see B. Reverting `replaced_by`/`superseded` would fail the existing assertions at [`test_auth.py:487`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:487>) and [`test_auth.py:522`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:522>), but they miss the remaining reset interval. |
| Hosted worker acquires its database’s lock or refuses | **DONE** | `run_forever()` derives `database.supervisor_lock_path` and refuses `None` before claiming work at [`worker.py:2612`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/jobs/worker.py:2612>). The regression invokes it without a lock argument and tests both acquisition and refusal at [`test_auth.py:951`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:951>). Reverting either branch deterministically fails those assertions. |
| NFC everywhere, bounded after normalization, no truncation | **DONE** | NFC is centralized at [`auth.py:104`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:104>), used by hashing and verification at [`auth.py:130`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:130>) and [`auth.py:617`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:617>), and by password/confirmation handling at [`hosted.py:334`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:334>). Changing it back to NFKC fails the compatibility-distinct assertions at [`test_accounts.py:453`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_accounts.py:453>). |

`git diff -- src/id_detector` is empty. Profiles, `README.md`, `idea.cmd`, playlists, `tests/test_playlists.py`, and `src/id_detector/present/theme.py` are untouched.

## B. Correctness bugs

- **Reset still has the stale-response cookie race.** `redeem_link()` commits password replacement and revokes every live session when its transaction exits at [`auth.py:792`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:792>)–[`auth.py:827`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:827>). Only afterwards does `password_post()` call `start_session()` to create the replacement and stamp `replaced_by` at [`hosted.py:352`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:352>)–[`hosted.py:360`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:360>).

  Concrete failure: reset transaction commits → concurrent request carrying the old cookie authenticates as dead → `finish()` sees no `replaced_by` yet and emits `Max-Age=0` at [`hosted.py:175`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:175>) → reset creates and returns the new session → responses arrive new-cookie first, clearing-cookie last → the browser loses the valid replacement.

- No stale password can create a session after reset: the credential-version predicate remains intact. No new money, cache, provider-attempt, cancellation, settlement, or payer-transfer defect was found.

## C. Scope

- Frozen core and all second-session-owned files are untouched.
- Migration `0006` is next, has a guarded operative down migration, and preserves pre-account opaque `jobs.user_id` values.
- Money-authority migrations and 4b-iv’s payer fence are unchanged. Worker changes wrap `run_forever()` with the supervisor lock; `LocalJobs` only forwards `user_id`.
- Tenancy, credit reservations, and result ownership remain assigned to 4d and are not omissions from 4c.

## D. Tests and gates

- The named phase suites exist and meaningfully cover argon2 parameters, cookie attributes, durable expiry, CSRF/Origin, rate limits, reset single-use races, admin gating, XSS, and no signup.
- No existing assertion was weakened; existing edits are schema-version updates or authenticated setup for the hosted parity test.
- The cookie regression is insufficiently ordered. Its first case starts the stale request only after the replacement response completed ([`test_auth.py:481`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:481>)); its threaded cases merely release both requests together without forcing the reset revocation/replacement interval ([`test_auth.py:494`](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:494>)).
- Every exact `uv` command failed before execution because the WinGet `uv.exe` alias could not launch. Direct pytest also could not start because the read-only sandbox has no writable temporary directory.
- Read-only fallbacks: Ruff check passed; Ruff format check with `--no-cache` reported 386 files formatted; fixture audit passed over 518 files; `git diff --check` passed. Page-JS could not create its temporary directory.

## E. Local mode

- The exact `uv run idea serve --no-open --port 8791` command could not launch because of the same `uv.exe` alias failure.
- A direct in-process local smoke returned 200 for `/`, 404 for `/login` and `/admin`, with no cookie or sign-in redirect; importing the local application loaded neither `argon2`, `idea_web.auth`, nor `idea_web.hosted`.
- `idea.cmd` is unchanged and local `ASSET_VERSION` remains `7ece3e97c2ae`.

## F. Quality

- The reset flow splits one security invariant across two transactions, despite `start_session()` correctly making its own insert/replacement operations atomic.
- The cookie test’s “ordered race” description overstates what it controls and allowed the reset gap to survive.
- No dead code or unrelated style drift was found.

## Required fixes

### Realistic

1. **P1:** Make reset redemption, old-session revocation, replacement-session insertion, and `replaced_by` stamping one database transaction, then add an HTTP-level barrier test that forces the stale request through the former gap and applies the replacement response before the stale response.

### Adversarial

None; no additional hostile-same-user local-mode issue is in scope.

## Follow-ups, not blockers

- Consider removing `run_forever(supervisor_lock=...)` or requiring it to equal `database.supervisor_lock_path`; a mismatched override could make the worker and migrator lock different files, although no in-repo caller currently passes one.
- Rerun all exact `uv` gates, page-JS, and the port-8791 smoke in a writable environment.
- Per-account job/result authorization remains a 4d deliverable.

VERDICT: FIX_FIRST