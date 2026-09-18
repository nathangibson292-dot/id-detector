## A. Contract violations

- **Export throttling is omitted.** §4.4 requires export routes at 10/min, but `RATE_LIMITS` contains only login/reset/analyse (`src/idea_web/auth.py:78-82`), and authenticated result/export GETs pass directly to `_get` without admission (`src/idea_web/application.py:672-692,426-441`).
- **Hosted migration lacks supervisor coordination.** Hosted startup invokes `database.migrate()` (`src/idea_web/application.py:781-785`), but supervisor acquisition returns `None` for ordinary hosted paths such as `/data/app.db` (`src/idea_web/database.py:294-307`). The CLI has the same issue (`src/idea_web/admin_cli.py:48-50`).
- Migration 0006 is correctly numbered and its down script appears structurally sound. Money-authority tables, profiles, `src/id_detector/**`, and all second-session-owned files are unchanged.

## B. Correctness bugs

- **P0 password-reset race:** login verification and session creation are separate transactions (`src/idea_web/hosted.py:206-215`). A request can verify the old password, then a reset can change the password and revoke all sessions (`src/idea_web/auth.py:725-755`), after which the first request blindly inserts a new live session (`src/idea_web/auth.py:622-642`). A compromised old password can therefore survive a reset.
- **Concurrent rotation can erase the winning cookie.** One request rotates the session while another receives `None`; the losing response unconditionally expires the cookie (`src/idea_web/hosted.py:118-126`). If that response arrives last, it clears the valid replacement and signs the browser out.
- **Real hosted submission has no compatible production adapter.** `_analyse` passes `user_id` (`src/idea_web/application.py:533-559`), but the declared protocol omits it (`src/idea_web/application.py:107-118`) and the only production `LocalJobs.submit` rejects it (`src/idea_web/jobs/local.py:287-295`). A signed-in submission using that adapter raises `TypeError`; the test fake alone accepts the argument (`tests/idea_web/hosted_helpers.py:76-85`).
- **Accepted Unicode passwords can be impossible to log in with.** Reset validates the NFKC-normalised length (`src/idea_web/hosted.py:270-273`), while login rejects on raw length and truncates before normalisation (`src/idea_web/auth.py:601-612`). For example, 1,200 combining-codepoint characters can normalise to 600, pass reset, then always fail login.
- No changed money, cache, attempt-state, cancellation, or settlement path was found.

## C. Scope

- No frozen profile, frozen engine, playlist, README, `idea.cmd`, or theme edit is present.
- The minimal entitlement/admin-plan work is defensible because §4.4 explicitly requires rotation on plan change.
- Hosted-only CSS is appended to the shared local stylesheet, changing local `ASSET_VERSION` and response bytes (`src/idea_web/pages.py:228-267`). It appears visually inert locally, but contradicts the claim that local assets are untouched.

## D. Tests and gates

- The named tests exist, are designed offline, and the reset-token test uses genuine thread/process races (`tests/idea_web/test_accounts.py:299-358`).
- Missing regressions: login-versus-reset race, HTTP-level rotation response ordering, real queue-adapter submission, export throttling, and long NFKC-normalising passwords.
- Existing changed tests retain their assertions; other edits are schema-version updates.
- All exact `uv` commands failed before execution because this sandbox cannot launch the WinGet `uv.exe` link. Direct pytest also could not start because the read-only environment has no writable temporary directory.
- Direct substitutes: Ruff passed, Ruff format passed, and fixture audit passed over 518 files. `check_page_js.py` also requires a writable temporary directory and could not start.

## E. Local mode

- `uv run idea serve --no-open --port 8791` could not launch because `uv.exe` is unavailable. Direct `idea serve` then failed before binding because the sandbox denied creation of `work/`.
- A read-only in-process smoke check returned `/` and `/healthz` as 200; `/login`, `/admin`, and `/reset/<token>` returned 404 with no cookies or sign-in redirect.
- Static inspection found no change to the serve entry point, audio routing, playlists, or truth-review implementation.

## F. Quality

- The queue protocol and its use disagree, while tests hide the defect with a broader fake.
- The session race test exercises only `AccountStore.authenticate`, not response-cookie ordering (`tests/idea_web/test_auth.py:385-414`).
- Hosted auth is imported eagerly by the local application, and hosted CSS is bundled into the local asset, weakening the intended local/hosted isolation.
- Reset secrets remain in `/reset/<token>` request paths (`src/idea_web/hosted.py:53,243,303`); any future hosted access logging must explicitly redact or disable request-target logging.

## Required fixes

### Realistic

1. **P0:** Make password verification/reset and session issuance version-checked or transactional so no session authenticated by an older password can be inserted after a password change.
2. **P1:** Add and test the required 10-per-minute hosted export limiter.
3. **P1:** Provide a real durable hosted queue adapter accepting `user_id`, update `JobQueueAdapter`, and test `/analyse` against it rather than only `RecordingJobs`.
4. **P1:** Prevent concurrent stale-session responses from clearing a successfully rotated cookie, with an HTTP-level response-order race test.
5. **P1:** Coordinate hosted startup and admin-CLI migrations with the hosted worker supervisor lock.
6. **P1:** Normalise a login password before applying the maximum-length check, without truncating it, and add a combining-character regression test.
7. **P2:** Separate hosted-only CSS/imports from local assets so local `ASSET_VERSION` and startup dependencies remain unchanged.
8. **P2:** Add a hosted access-log test proving setup/reset bearer tokens are redacted or access logging is disabled.

### Adversarial

- No additional same-user-hostile-program fix is required beyond the explicitly out-of-scope ability of such a program to inspect or alter the owner’s local process and files.

VERDICT: FIX_FIRST