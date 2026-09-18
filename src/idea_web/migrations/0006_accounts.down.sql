-- Fail closed (as 0003 and 0005): an account is who a job, a run or an audit row names, and a
-- plan is what an admin granted. Dropping them while any exists would orphan that history, so the
-- downgrade aborts (and the migration runner rolls back) while any account or plan row exists.
-- Sessions, links and limit counters cannot exist without an account, except limit counters,
-- which are disposable.
CREATE TEMP TABLE accounts_guard (probe INTEGER);
CREATE TEMP TRIGGER accounts_guard_check BEFORE INSERT ON accounts_guard
WHEN EXISTS (SELECT 1 FROM users)
    OR EXISTS (SELECT 1 FROM entitlements)
    OR EXISTS (SELECT 1 FROM sessions)
    OR EXISTS (SELECT 1 FROM email_tokens)
BEGIN
    SELECT RAISE(ABORT, 'refusing to downgrade: the account tables hold rows');
END;
INSERT INTO accounts_guard(probe) VALUES (1);
DROP TRIGGER accounts_guard_check;
DROP TABLE accounts_guard;
DROP INDEX IF EXISTS rate_limit_hits_window;
DROP TABLE IF EXISTS rate_limit_hits;
DROP INDEX IF EXISTS entitlements_one_active;
DROP TABLE IF EXISTS entitlements;
DROP TRIGGER IF EXISTS email_tokens_single_use;
DROP INDEX IF EXISTS email_tokens_one_outstanding;
DROP TABLE IF EXISTS email_tokens;
DROP TRIGGER IF EXISTS sessions_revocation_is_final;
DROP INDEX IF EXISTS sessions_user;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;
