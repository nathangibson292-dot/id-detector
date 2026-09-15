-- Fail closed (round 6): these tables are the ONLY durable proof that a paid request left or a run
-- settled. Dropping a populated table would let the 0002 code re-send a clip it cannot see, so the
-- downgrade aborts (and the migration runner rolls back) while any of them holds a row.
CREATE TEMP TABLE money_authority_guard (probe INTEGER);
CREATE TEMP TRIGGER money_authority_guard_check BEFORE INSERT ON money_authority_guard
WHEN EXISTS (SELECT 1 FROM run_dispatches)
    OR EXISTS (SELECT 1 FROM run_settlements)
    OR EXISTS (SELECT 1 FROM run_reservations)
BEGIN
    SELECT RAISE(ABORT, 'refusing to downgrade: the money authority tables hold rows');
END;
INSERT INTO money_authority_guard(probe) VALUES (1);
DROP TRIGGER money_authority_guard_check;
DROP TABLE money_authority_guard;
DROP TRIGGER IF EXISTS jobs_money_authority_provenance;
ALTER TABLE jobs DROP COLUMN authority_token;
ALTER TABLE jobs DROP COLUMN money_authority;
DROP TABLE IF EXISTS run_reservations;
DROP TABLE IF EXISTS run_settlements;
DROP TABLE IF EXISTS run_dispatches;
