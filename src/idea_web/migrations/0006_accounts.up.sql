-- Accounts, sessions, single-use links, plans and per-IP limits (plan §3.6, §4.4, §4.5; cycles
-- 4c-i and 4c-ii). These tables are HOSTED-mode state: a local `idea serve` migrates them into its
-- queue database like every other migration but never reads or writes them, so local mode stays
-- sign-in free.
--
-- `jobs.user_id` (0005) stays a plain TEXT column with no foreign key: rows written before this
-- migration keep whatever opaque id they carried. From here on a hosted submission stores the
-- signed-in account's `users.id` there.
--
-- Secrets are never stored: a session is found by `sha256(cookie token)`, a single-use link by
-- `sha256(link token)`, and a password only as its argon2id hash.

CREATE TABLE users (
    -- An opaque id ('u' + 32 hex digits); the value a hosted job's `jobs.user_id` carries.
    id TEXT PRIMARY KEY,
    -- Stored normalised (trimmed, lower-cased); one account per address.
    email TEXT NOT NULL UNIQUE,
    -- argon2id (idea_web.auth.PASSWORD_HASHER). NULL until the owner of an admin-created account
    -- sets one through the single-use link the admin handed over; such an account cannot sign in.
    pw_hash TEXT,
    -- The credential's version: +1 every time the password is set. A session is inserted only
    -- if the version a sign-in verified is still current, so a sign-in that checked a password
    -- a reset has since replaced issues nothing (idea_web.auth.AccountStore.start_session).
    pw_version INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    -- M1 accounts are admin-created and pre-verified (plan §3.6), so this is set at creation.
    verified_at REAL,
    created_ip TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    password_changed_at REAL,
    disabled_at REAL,
    deleted_at REAL
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    -- sha256(token) in lower-case hex; the raw token lives only in the browser's cookie.
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    -- Idle expiry is measured from here (30 days).
    last_seen_at REAL NOT NULL,
    -- Absolute expiry (90 days after sign-in). A rotated session inherits it unchanged.
    expires_at REAL NOT NULL,
    -- Set by a plan change: the next request that presents this session gets a new one.
    rotate_required INTEGER NOT NULL DEFAULT 0 CHECK (rotate_required IN (0, 1)),
    revoked_at REAL,
    revoked_reason TEXT,
    replaced_by INTEGER REFERENCES sessions(id),
    created_ip TEXT,
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);

CREATE INDEX sessions_user ON sessions(user_id, revoked_at);

-- A revoked session stays revoked.
CREATE TRIGGER sessions_revocation_is_final
BEFORE UPDATE OF revoked_at ON sessions
WHEN OLD.revoked_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a revoked session cannot be reinstated');
END;

-- Single-use links (plan §4.5 `email_tokens`). In M1 an admin issues them: `setup` for a new
-- account, `reset` for a forgotten password. Invites arrive with M2 7c.
CREATE TABLE email_tokens (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    user_id TEXT NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('setup', 'reset')),
    issued_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    revoked_at REAL,
    CHECK (used_at IS NULL OR revoked_at IS NULL)
);

-- At most one outstanding link per account: issuing a new one revokes the old one first.
CREATE UNIQUE INDEX email_tokens_one_outstanding
    ON email_tokens(user_id) WHERE used_at IS NULL AND revoked_at IS NULL;

-- A link is used at most once, whatever the application does.
CREATE TRIGGER email_tokens_single_use
BEFORE UPDATE OF used_at, revoked_at ON email_tokens
WHEN OLD.used_at IS NOT NULL OR OLD.revoked_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a used or revoked link cannot be changed');
END;

-- Plan §4.5 `entitlements`: the account's plan. In M1 every plan is set by an admin; billing
-- (M2) adds other sources. No active row means Free.
CREATE TABLE entitlements (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    plan TEXT NOT NULL CHECK (plan IN ('free', 'pro')),
    status TEXT NOT NULL CHECK (status IN ('active', 'ended')),
    source TEXT NOT NULL DEFAULT 'admin' CHECK (source = 'admin'),
    period_end REAL,
    set_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    ended_at REAL,
    CHECK ((status = 'ended') = (ended_at IS NOT NULL))
);

CREATE UNIQUE INDEX entitlements_one_active ON entitlements(user_id) WHERE status = 'active';

-- Per-IP request limits (plan §4.4). One row per admitted attempt; a bucket is an IPv4 address
-- or an IPv6 /64. Rows older than the action's window are pruned as the bucket is checked.
CREATE TABLE rate_limit_hits (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    bucket TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE INDEX rate_limit_hits_window ON rate_limit_hits(action, bucket, at);
