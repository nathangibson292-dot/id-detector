-- Fail closed (as 0003): these rows are the only record of who reserved and who pays for a run.
-- Dropping them while any exists would leave a run with a payer nobody can account for, so the
-- downgrade aborts (and the migration runner rolls back) while any of them holds a row.
CREATE TEMP TABLE run_subscribers_guard (probe INTEGER);
CREATE TEMP TRIGGER run_subscribers_guard_check BEFORE INSERT ON run_subscribers_guard
WHEN EXISTS (SELECT 1 FROM run_subscribers)
    OR EXISTS (SELECT 1 FROM run_payer_events)
    OR EXISTS (SELECT 1 FROM admin_audit)
BEGIN
    SELECT RAISE(ABORT, 'refusing to downgrade: the subscriber and payer tables hold rows');
END;
INSERT INTO run_subscribers_guard(probe) VALUES (1);
DROP TRIGGER run_subscribers_guard_check;
DROP TABLE run_subscribers_guard;
DROP TRIGGER IF EXISTS admin_audit_no_delete;
DROP TRIGGER IF EXISTS admin_audit_no_update;
DROP TABLE IF EXISTS admin_audit;
DROP TRIGGER IF EXISTS run_payer_events_no_delete;
DROP TRIGGER IF EXISTS run_payer_events_no_update;
DROP TABLE IF EXISTS run_payer_events;
DROP TRIGGER IF EXISTS run_subscribers_no_delete;
DROP TRIGGER IF EXISTS run_subscribers_attach_guard;
DROP TABLE IF EXISTS run_subscribers;
ALTER TABLE jobs DROP COLUMN user_id;
