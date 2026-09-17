BEGIN TRANSACTION;
CREATE TABLE analysis_runs (
    run_id TEXT PRIMARY KEY,
    analysis_key TEXT NOT NULL,
    media_key TEXT NOT NULL REFERENCES media(media_key),
    requested_recipe_id TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    adapter_versions TEXT NOT NULL,
    achieved TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    tenant_scope TEXT NOT NULL,
    payer_user TEXT,
    payer_reservation_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    checkpoints TEXT NOT NULL DEFAULT '{}',
    usd_e6_reserved INTEGER NOT NULL DEFAULT 0 CHECK (usd_e6_reserved >= 0),
    usd_e6_spent INTEGER NOT NULL DEFAULT 0 CHECK (usd_e6_spent >= 0),
    pricing_version TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    analysis_inputs TEXT NOT NULL,
    non_recipe_key TEXT NOT NULL,
    -- The fence for every checkpoint, money and journal write on this run: the claim generation
    -- of the job currently executing it. Rotated on each claim, cleared on a terminal settlement
    -- or a return to the queue, so a reclaimed worker can never overwrite its replacement.
    claim_token TEXT
);
INSERT INTO "analysis_runs" VALUES('fixture-run-3','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','fixture-media','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','1','{}','free','complete',NULL,'public',NULL,NULL,0,'{}',0,0,NULL,1000.0,1000.0,'{}','cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',NULL);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    target TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'intake', 'waiting', 'analysis', 'complete', 'degraded', 'partial',
            'provider_unavailable', 'budget_exhausted', 'source_changed',
            'quota_exceeded', 'failed', 'cancelled', 'dead_letter'
        )
    ),
    lease_owner TEXT,
    lease_until REAL,
    heartbeat_at REAL,
    -- Unique per claim, not per worker: a restarted worker that reuses its id cannot pass the
    -- fence of the claim it lost, and a stale writer is rejected rather than believed.
    claim_token TEXT,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts = 3),
    dead_letter_reason TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    progress TEXT NOT NULL DEFAULT '{}',
    log_path TEXT,
    result_bundle_id TEXT REFERENCES result_bundles(bundle_id),
    tenant_scope TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL, attached INTEGER NOT NULL DEFAULT 0, money_authority INTEGER, authority_token TEXT,
    CHECK (attempt <= max_attempts)
);
CREATE TABLE media (
    media_key TEXT PRIMARY KEY,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    first_seen REAL NOT NULL
);
INSERT INTO "media" VALUES('fixture-media',600000,1000.0);
CREATE TABLE provider_attempt_events (
    event_id INTEGER PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 3),
    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    provider TEXT NOT NULL,
    egress_id TEXT NOT NULL,
    -- The clip cache key when the provider seam carries one. The Shazam seam (§2.3.5's breaker
    -- denominator) is the process breaker, which knows the egress and the outcome but not the
    -- query, so its rows carry NULL rather than an invented identity.
    query_id TEXT,
    parent_attempt_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'dispatched', 'resolved')),
    outcome TEXT,
    http_status INTEGER,
    unit_usd_e6 INTEGER NOT NULL CHECK (unit_usd_e6 >= 0),
    at TEXT NOT NULL,
    UNIQUE(attempt_id, seq),
    CHECK ((state = 'resolved') = (outcome IS NOT NULL)),
    CHECK (provider <> 'audd' OR query_id IS NOT NULL)
);
CREATE TABLE provider_breaker_state (
    provider TEXT NOT NULL,
    egress_id TEXT NOT NULL,
    -- The UTC day `opens` counts within; a new day resets the count but never the latch, which is
    -- off "until an admin re-enables" (§2.3.5 rule (c), owner decision D8).
    day TEXT NOT NULL,
    opens INTEGER NOT NULL DEFAULT 0 CHECK (opens >= 0),
    open_until REAL,
    latched INTEGER NOT NULL DEFAULT 0 CHECK (latched IN (0, 1)),
    reenable_generation INTEGER NOT NULL DEFAULT 0 CHECK (reenable_generation >= 0),
    -- When an operator last re-enabled this provider. Rule (a) ignores every sample at or before
    -- it: the failures that caused the trip are still inside the five-minute window, so without a
    -- cutoff a re-enable would be undone by its own history on the very next judgement. The
    -- process breaker had the same effect by clearing its sample deque.
    reenabled_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (provider, egress_id)
);
CREATE TABLE result_bundles (
    bundle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    presentation_version INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
INSERT INTO "result_bundles" VALUES('6d1849fa08c49c14d9bd6af139bd2636c07854b4741e986b30b54799bf494cd3','fixture-run-3',1,'/idea-fixture-work/fixture-source/fixture-media/present/bundles/6d1849fa08c49c14d9bd6af139bd2636c07854b4741e986b30b54799bf494cd3','5229da4261592611b709c37d12aa2cc7358ab5570b86cc0cc4d7b8f54e277cb0',1000.0);
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
CREATE TABLE run_reservations (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    reservation TEXT NOT NULL,
    usd_e6_reserved INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE run_settlements (
    run_id TEXT PRIMARY KEY,
    job_id TEXT,
    status TEXT NOT NULL,
    journal_path TEXT NOT NULL,
    entry TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE schema_migrations (number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES(1,'durable_queue','2026-09-16T04:02:09.277Z');
INSERT INTO "schema_migrations" VALUES(2,'job_ownership','2026-09-16T04:02:09.277Z');
INSERT INTO "schema_migrations" VALUES(3,'money_authority','2026-09-16T04:02:09.278Z');
INSERT INTO "schema_migrations" VALUES(4,'shared_breaker','2026-09-16T04:02:09.278Z');
CREATE INDEX analysis_runs_lookup
    ON analysis_runs(media_key, tenant_scope, status, started_at);
CREATE INDEX analysis_runs_coalescing
    ON analysis_runs(analysis_key, status);
CREATE INDEX result_bundles_run ON result_bundles(run_id, created_at);
CREATE INDEX jobs_claimable ON jobs(state, lease_until, created_at);
CREATE INDEX jobs_run ON jobs(run_id);
CREATE INDEX provider_attempt_events_breaker
    ON provider_attempt_events(provider, egress_id, at);
CREATE INDEX provider_attempt_events_run
    ON provider_attempt_events(run_id, event_id);
CREATE TRIGGER provider_attempt_events_no_update
BEFORE UPDATE ON provider_attempt_events
BEGIN
    SELECT RAISE(ABORT, 'provider_attempt_events is append-only');
END;
CREATE TRIGGER provider_attempt_events_no_delete
BEFORE DELETE ON provider_attempt_events
BEGIN
    SELECT RAISE(ABORT, 'provider_attempt_events is append-only');
END;
CREATE TRIGGER jobs_money_authority_provenance
AFTER UPDATE OF claim_token ON jobs
WHEN NEW.claim_token IS NOT NULL
    AND NEW.claim_token IS NOT OLD.claim_token
    AND NEW.authority_token IS NOT NEW.claim_token
BEGIN
    UPDATE jobs SET money_authority = 0 WHERE id = NEW.id;
END;
COMMIT;
