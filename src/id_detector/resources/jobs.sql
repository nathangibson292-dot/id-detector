PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    media_key TEXT NOT NULL,
    query_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'pending', 'leased', 'submission_started', 'submitted', 'succeeded', 'no_match',
        'retryable_failure', 'permanent_failure', 'outcome_unknown', 'cancelled'
    )),
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    attempts INTEGER NOT NULL,
    physical_attempts INTEGER NOT NULL,
    next_retry_at TEXT,
    submission_started_at TEXT,
    submitted_at TEXT,
    remote_ref TEXT,
    reserved_units INTEGER NOT NULL,
    reserved_usd INTEGER NOT NULL,
    actual_units INTEGER NOT NULL,
    actual_usd INTEGER NOT NULL,
    result_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE budgets (
    media_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    max_requests INTEGER NOT NULL,
    max_usd INTEGER NOT NULL,
    reserved_requests INTEGER NOT NULL,
    reserved_usd INTEGER NOT NULL,
    used_requests INTEGER NOT NULL,
    used_usd INTEGER NOT NULL,
    PRIMARY KEY (media_key, provider)
);

CREATE TABLE connector_jobs (
    id TEXT PRIMARY KEY,
    media_key TEXT NOT NULL,
    connector TEXT NOT NULL,
    target_url TEXT NOT NULL,
    cursor TEXT,
    page INTEGER NOT NULL,
    page_cap INTEGER NOT NULL,
    item_cap INTEGER NOT NULL,
    items_fetched INTEGER NOT NULL,
    state TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    attempts INTEGER NOT NULL,
    next_retry_at TEXT,
    result_path TEXT,
    truncated INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
