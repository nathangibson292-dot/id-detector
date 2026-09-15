-- SQLite is the single authority for paid dispatch and terminal settlement (round-5 design).
--
-- A paid request may leave only once its `run_dispatches` row has committed, in the SAME
-- transaction as the job's claim check (claim token, active state, unexpired lease, run token,
-- no cancellation request). The attempt JSONL line is a projection written after that commit.
-- The primary key makes a second dispatch of one attempt impossible: the second insert is a no-op,
-- and a no-op insert is a refusal to send.
CREATE TABLE run_dispatches (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    event TEXT NOT NULL,
    journal_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, attempt_id)
);

-- One terminal settlement per run. The claim holder upserts it under its fence (monotonic money);
-- a derived sweep or a post-commit fast path only inserts when no row exists. invocations.jsonl
-- is re-projected from this row whenever its line is missing.
CREATE TABLE run_settlements (
    run_id TEXT PRIMARY KEY,
    job_id TEXT,
    status TEXT NOT NULL,
    journal_path TEXT NOT NULL,
    entry TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- One reservation per run (round-6 design): written in the SAME transaction as the job's claim
-- check, before the run's first paid dispatch. Resume reads this row first; the JSON file beside
-- the attempt journal is only its projection. A run with dispatch rows and no reservation row
-- refuses every further paid dispatch rather than recomputing from today's price or cap.
CREATE TABLE run_reservations (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    reservation TEXT NOT NULL,
    usd_e6_reserved INTEGER NOT NULL,
    created_at REAL NOT NULL
);

-- Durable money-authority provenance (round-7 design). A job stamped `money_authority = 3` has had
-- every claim made by code that writes the authority rows above, so "no dispatch and no reservation
-- row" proves it spent nothing. Such code writes `authority_token = claim_token` in the SAME claim
-- UPDATE; a claim written by older code (which knows neither column) leaves them unequal, and this
-- trigger then marks the job 0 for good, so it can never receive a zero-money settlement.
ALTER TABLE jobs ADD COLUMN money_authority INTEGER;
ALTER TABLE jobs ADD COLUMN authority_token TEXT;
CREATE TRIGGER jobs_money_authority_provenance
AFTER UPDATE OF claim_token ON jobs
WHEN NEW.claim_token IS NOT NULL
    AND NEW.claim_token IS NOT OLD.claim_token
    AND NEW.authority_token IS NOT NEW.claim_token
BEGIN
    UPDATE jobs SET money_authority = 0 WHERE id = NEW.id;
END;
