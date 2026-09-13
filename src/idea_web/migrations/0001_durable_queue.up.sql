CREATE TABLE media (
    media_key TEXT PRIMARY KEY,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    first_seen REAL NOT NULL
);

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

CREATE INDEX analysis_runs_lookup
    ON analysis_runs(media_key, tenant_scope, status, started_at);
CREATE INDEX analysis_runs_coalescing
    ON analysis_runs(analysis_key, status);

CREATE TABLE result_bundles (
    bundle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    presentation_version INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX result_bundles_run ON result_bundles(run_id, created_at);

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
    updated_at REAL NOT NULL,
    CHECK (attempt <= max_attempts)
);

CREATE INDEX jobs_claimable ON jobs(state, lease_until, created_at);
CREATE INDEX jobs_run ON jobs(run_id);

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
