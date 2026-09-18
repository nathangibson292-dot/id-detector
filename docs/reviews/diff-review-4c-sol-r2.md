## A. Contract violations and round-1 confirmation

| # | Status | Evidence and reversion coverage |
|---|---|---|
| 1 | **DONE** | Session insertion atomically requires the verified `pw_version` ([src/idea_web/auth.py:655](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:655>)); reset increments it and revokes sessions and links ([auth.py:797](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:797>)). The barrier test asserts stale login gets 401 and cannot create a session ([tests/idea_web/test_accounts.py:365](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_accounts.py:365>)); making the version predicate vacuous necessarily fails those assertions. |
| 2 | **DONE** | Export limit is 10/min ([auth.py:78](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:78>)), enforced before export delivery ([application.py:716](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/application.py:716>)), and tested through ten successes plus an eleventh 429 ([test_auth.py:706](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:706>)). Removing admission makes that test fail. |
| 3 | **DONE** | The existing `LocalJobs` adapter passes `user_id` directly to `JobQueue.enqueue` ([src/idea_web/jobs/local.py:293](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/jobs/local.py:293>), [local.py:321](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/jobs/local.py:321>)). The real-adapter test verifies durable `jobs.user_id` rows ([test_auth.py:809](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:809>)). No second queue exists. |
| 4 | **PARTIAL** | Plan-change rotation is protected and tested, but login/reset rotations are not; see B. Reverting `rotated_away` fails the current plan-change test, but that test does not cover the remaining races. |
| 5 | **PARTIAL** | Hosted startup and the CLI use a supervised database ([database.py:370](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/database.py:370>), [admin_cli.py:48](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/admin_cli.py:48>)), but the worker lock remains optional; see B. |
| 6 | **PARTIAL** | All password paths consistently use NFKC and bound the normalized value without truncation ([auth.py:104](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:104>), [auth.py:614](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:614>), [hosted.py:334](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:334>)). The old raw-length/truncation behavior would fail the regression, but NFKC itself weakens exact credentials; see B. |
| 7 | **DONE** | Hosted CSS is separate ([pages.py:224](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/pages.py:224>)); auth imports are lazy ([application.py:69](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/application.py:69>)). Direct inspection produced unchanged `ASSET_VERSION=7ece3e97c2ae` and loaded none of `argon2`, `idea_web.auth`, or `idea_web.hosted`. |
| 8 | **DONE** | Hosted startup installs reset-path redaction ([hosted.py:62](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:62>), [application.py:821](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/application.py:821>)); the regression uses real uvicorn access logging ([test_auth.py:973](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:973>)). Removing the filter exposes the token and fails it. |

Mutation runs could not be independently executed in this read-only environment; the reversion statements above are code-path confirmations, not reliance on the builder’s pasted output.

## B. Correctness bugs

- **Login/reset rotation can still clear the winning cookie.** `finish()` suppresses clearing only when the old row has `revoked_reason='rotated'` and `replaced_by` ([hosted.py:162](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/hosted.py:162>), [auth.py:737](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:737>)). Login instead revokes the previous session as `replaced` without `replaced_by` ([auth.py:671](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:671>)); reset revokes it as `password` ([auth.py:803](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/auth.py:803>)). Thus: response A sets replacement N; a concurrent response using old S authenticates after revocation, emits `Max-Age=0`; if it arrives last, it deletes N. This is the same round-1 race across two required rotation paths.

- **The migration/worker invariant is opt-in.** `Worker.run_forever(supervisor_lock=None)` acquires nothing unless its caller supplies a path ([src/idea_web/jobs/worker.py:2589](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/src/idea_web/jobs/worker.py:2589>)). The regression explicitly supplies the lock ([test_auth.py:906](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:906>)), hiding that `Worker(hosted_database(...), ...).run_forever()` is unsupervised and allows a concurrent migration.

- **NFKC creates alternate passwords.** It maps `①→1`, `Ⅳ→IV`, `ﬀ→ff`, and full-width text to ASCII. Because hashing and verification normalize both inputs, choosing one exact string also permits its compatibility-equivalent string. NFC retains canonical composition without creating these broad credential aliases.

- No stale credential can create a session after a password reset through the reviewed API. No new settlement, payer-transfer, provider-attempt, cache, or cancellation defect was found.

## C. Scope

- `git diff -- src/id_detector` is exactly 0 lines. Profiles, `README.md`, `idea.cmd`, playlists, `tests/test_playlists.py`, and `src/id_detector/present/theme.py` are untouched.
- Migration `0006` is next, has an operative guarded down script, and leaves old opaque `jobs.user_id` values valid.
- Money-authority tables and 4b-iv’s payer fence are unchanged in substance. Worker changes concern only supervisor locking; `LocalJobs` only forwards the existing `user_id`.
- Tenancy remains explicitly assigned to 4d, so its absence is not charged to this phase.

## D. Tests and gates

- The named phase tests exist, are offline, and meaningfully assert pre-verification, single-use reset races, escaping, no signup, cookie properties, expiry, CSRF, limits, and durable queue use. Existing changed assertions were version updates or authentication setup; none was weakened.
- Missing regressions: ordered login/reset cookie races, a hosted worker started without an explicit lock argument, and proof that compatibility-distinct passwords remain distinct.
- Every exact `uv` command requested—including full pytest, both phase gates, `tests/idea_web`, Ruff, fixture audit, page-JS check, and local serve—failed before execution because the sandbox cannot launch the WinGet `uv.exe` alias.
- Direct fallbacks: Ruff passed; fixture audit passed over 518 files. Pytest and the page-JS checker could not start because the sandbox has no writable temporary directory. `git diff --check` passed.

## E. Local mode

- The exact `uv run idea serve --no-open --port 8791` smoke test could not start for the `uv.exe` reason above.
- A read-only in-process local smoke returned 200 for `/` and `/healthz`; `/login`, `/admin`, and `/reset/<token>` returned 404, with no cookie or sign-in redirect.
- Local assets/imports are unchanged, and `idea.cmd`, audio routing, playlists, and truth-review files have no diff.

## F. Quality

- The reason-specific cookie-clearing logic is under-specified: its regression name claims all “freshly rotated” cookies while testing only plan-change rotation ([test_auth.py:425](<C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a83691e62b2fda9d3/tests/idea_web/test_auth.py:425>)).
- The worker-lock test proves only correct manual use, while the production API defaults to unsafe use.
- The normalization regression proves composition, bounds, and no truncation, but omits compatibility-character collisions.

## Required fixes

### Realistic

1. **P1:** Preserve replacement cookies for login and reset as well as plan changes, and add HTTP-level ordered races for all three paths.
2. **P1:** Make a hosted `Worker.run_forever()` automatically use `database.supervisor_lock_path` or otherwise refuse to run unsupervised; test it without passing the lock explicitly.
3. **P1:** Replace NFKC with NFC consistently across setup/reset, hashing, sign-in, confirmation, and normalized-length checks, with a regression proving compatibility-distinct passwords do not authenticate each other.

### Adversarial

- No additional hostile-same-user local-mode fix is required; that attacker remains outside the stated local threat model.

VERDICT: FIX_FIRST