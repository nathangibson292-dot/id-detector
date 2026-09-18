## A. Contract violations

1. **DONE — atomic reset/replacement.** `AccountStore.redeem_and_start_session()` performs redemption and replacement issuance within one `Database.write()` transaction (`src/idea_web/auth.py:829`–`838`); that context uses `BEGIN IMMEDIATE` and commits only after the body completes (`src/idea_web/database.py:212`–`221`). Redemption revokes sessions at `auth.py:841`–`876`, while replacement insertion and `replaced_by` stamping occur at `auth.py:674`–`702`. The HTTP route uses this combined operation at `src/idea_web/hosted.py:352`–`368`.

2. **DONE — worker/migrator lock identity.** `run_forever()` derives `database.supervisor_lock_path`, refuses unsupervised databases, and rejects any unequal override before acquiring or claiming work (`src/idea_web/jobs/worker.py:2613`–`2625`). The regression checks mismatched rejection, matching acceptance, live migration exclusion, second-worker exclusion, and release (`tests/idea_web/test_auth.py:1034`–`1082`).

- `git diff -- src/id_detector` is 0 lines. No frozen profile, golden, or fixture changed.
- The second-session-owned paths, including `idea.cmd`, are unchanged.
- No D1–D8 conflict or stale/revoked-credential regression was found.

## B. Correctness bugs

- No realistic correctness defect introduced by Fix pass 3 was found.
- The reset barrier test genuinely exposes the former gap: it pauses immediately after the first commit that revokes the old session (`tests/idea_web/test_auth.py:578`–`595`), sends and completes the stale request before reset continues (`test_auth.py:598`–`615`), then applies the replacement response first and proves the new cookie survives (`test_auth.py:616`–`629`).
- The password credential-version condition remains enforced during replacement insertion (`src/idea_web/auth.py:675`–`690`).
- Money authority, reservations, caps, attempt journals, settlement, cancellation, and the 4b-iv payer fence have no substantive diff.

## C. Scope

- HEAD is `f913e91`; all changes remain uncommitted.
- Migration `0006` is next and its down migration refuses account-bearing databases while leaving money-authority tables intact (`src/idea_web/migrations/0006_accounts.down.sql:1`).
- Existing test edits are migration-version updates plus authenticated setup for the hosted parity check. No test or assertion was deleted.
- No missing 4c requirement or out-of-phase change was identified under the Round 4 stop rule.

## D. Tests

- The named suites collect successfully: 28 auth tests and 11 account tests; all `tests/idea_web` collect successfully with 373 tests.
- Coverage is substantive: real threaded/process token races (`tests/idea_web/test_accounts.py:304`–`363`), stale-login fencing (`test_accounts.py:366`), XSS assertions (`test_accounts.py:496`–`546`), no-signup coverage (`test_accounts.py:550`–`605`), and the new ordered HTTP reset race (`tests/idea_web/test_auth.py:550`–`629`).
- Every exact `uv` command failed before execution because Windows resolves `uv.exe` to a broken WinGet alias. Direct pytest and page-JS execution were then blocked because the read-only sandbox has no writable temporary directory.
- Direct fallbacks: Ruff check passed with `--no-cache`; fixture audit passed over 518 files; `git diff --check` passed.

## E. Local mode

- The exact `uv run idea serve --no-open --port 8791` could not launch because of the broken `uv` alias. The direct entry point reached local queue initialization but the sandbox denied creating `work/`.
- A read-only in-process smoke returned `/` = 200, `/login` = 404, `/admin` = 404, with no redirect or `Set-Cookie`; auth, hosted, and argon2 modules were not imported.
- The local/hosted branch remains explicit at `src/idea_web/application.py:797`–`826`. The comprehensive local contract test asserts no sign-in, cookie, 401, or login redirect at `tests/idea_web/test_auth.py:1201`–`1244`.
- `idea.cmd` is unchanged.

## F. Quality

- `_redeem()` and `_issue_session()` centralize the transaction-sensitive operations instead of duplicating them (`src/idea_web/auth.py:663`, `841`).
- The worker override remains only as a validated compatibility seam and has direct mismatch coverage.
- No dead code, unrelated style drift, weakened assertion, or missing regression for either Round 3 item was found.

## Required fixes

### Realistic

None.

### Adversarial

None.

## Follow-ups, not blockers

1. **P2:** Re-run the exact full suite, phase gates, page-JS check, and port-8791 smoke in a writable environment with a functioning `uv.exe`.

VERDICT: OK_TO_COMMIT