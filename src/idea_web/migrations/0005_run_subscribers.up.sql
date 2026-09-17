-- Coalescing, subscribers and payer transfer (plan §3.4, §3.5, §4.5; cycle 4b-iv).
--
-- There are no accounts until 4c-i and no credit lots until 4d-i, so a "user" is an opaque text id
-- carried by the job that asked (`jobs.user_id`, NULL for an anonymous or local request), and a
-- subscriber's credit reservation is the `run_subscribers` row itself: an id, a size in whole mix
-- minutes, and a state. 4d-i backs the same rows with lots and allocations; nothing here is money.
--
-- Who pays for a run is `analysis_runs.payer_user` / `payer_reservation_id` (columns since 0001),
-- changed only inside one `BEGIN IMMEDIATE` transaction together with the subscriber rows and an
-- append-only `run_payer_events` row. The run's USD money itself stays where 0003 put it
-- (`run_reservations`, `run_dispatches`, `run_settlements`, the attempt events): a transfer
-- re-attributes that money to another account, it never computes or settles a second figure.
ALTER TABLE jobs ADD COLUMN user_id TEXT;

CREATE TABLE run_subscribers (
    -- Attachment order: "the earliest-attached remaining subscriber" is the lowest `seq`.
    seq INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    -- A job subscribes to at most one run, once.
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    user_id TEXT,
    -- The recipe the subscriber asked (and would pay) for. It must be the run's own recipe, so a
    -- Free request can never hold a reservation on a paid run, nor a paid one on a Free run.
    recipe_id TEXT NOT NULL,
    -- `source_aliases` arrives with 6a-i; until then no alias is recorded.
    alias_id TEXT,
    minutes_reserved INTEGER NOT NULL CHECK (minutes_reserved >= 0),
    -- held: reserved (the payer's is the settling one, every other one is provisional);
    -- released: returned in full (a detach, a transfer away, or a non-payer at the terminal status);
    -- settled: the payer's reservation at the run's terminal status.
    reservation_state TEXT NOT NULL DEFAULT 'held'
        CHECK (reservation_state IN ('held', 'released', 'settled')),
    attached_at REAL NOT NULL,
    detached_at REAL,
    released_at REAL,
    CHECK ((reservation_state = 'held') = (released_at IS NULL))
);

CREATE INDEX run_subscribers_run ON run_subscribers(run_id, reservation_state, seq);

-- A subscriber is only ever attached to a live run, for the run's own recipe; a private run has
-- exactly one subscriber, its initiator (plan §3.4: private scope never coalesces).
CREATE TRIGGER run_subscribers_attach_guard
BEFORE INSERT ON run_subscribers
BEGIN
    SELECT RAISE(ABORT, 'subscriber refused: unknown or finished run')
    WHERE NOT EXISTS (
        SELECT 1 FROM analysis_runs
        WHERE run_id = NEW.run_id AND status IN ('intake', 'waiting', 'analysis')
    );
    SELECT RAISE(ABORT, 'subscriber refused: recipe differs from the run''s')
    WHERE NEW.recipe_id IS NOT (
        SELECT requested_recipe_id FROM analysis_runs WHERE run_id = NEW.run_id
    );
    SELECT RAISE(ABORT, 'subscriber refused: a private run never coalesces')
    WHERE (SELECT tenant_scope FROM analysis_runs WHERE run_id = NEW.run_id) <> 'public'
        AND EXISTS (SELECT 1 FROM run_subscribers WHERE run_id = NEW.run_id);
END;

CREATE TRIGGER run_subscribers_no_delete
BEFORE DELETE ON run_subscribers
BEGIN
    SELECT RAISE(ABORT, 'run_subscribers rows are never deleted');
END;

-- Every change of who holds or pays for a run, append-only. The unique key makes each kind of
-- change happen at most once per reservation: a reservation is attached, detached, transferred
-- away from, released and closed at most once each, so two concurrent detaches can never produce
-- two transfers even if both reached the write.
CREATE TABLE run_payer_events (
    event_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
    kind TEXT NOT NULL CHECK (
        kind IN ('attach', 'detach', 'transfer', 'reserve', 'release', 'close')
    ),
    reservation_id TEXT NOT NULL REFERENCES run_subscribers(reservation_id),
    user_id TEXT,
    to_reservation_id TEXT REFERENCES run_subscribers(reservation_id),
    to_user_id TEXT,
    -- transfer: the run's USD reservation at that moment, re-attributed to `to_user_id`;
    -- reserve: the run's USD reservation when it is created, attributed to whoever pays at that
    -- moment (`reservation_id`), so a transfer that happened before the reservation existed never
    -- leaves the money unattributed. Both carry the part the payer's month cap could not cover
    -- (charged against the global pool, audited).
    usd_e6_reattributed INTEGER NOT NULL DEFAULT 0 CHECK (usd_e6_reattributed >= 0),
    usd_e6_overage INTEGER NOT NULL DEFAULT 0 CHECK (usd_e6_overage >= 0),
    -- close: the run's terminal status the payer's reservation settles under.
    status TEXT,
    at REAL NOT NULL,
    UNIQUE (run_id, kind, reservation_id),
    CHECK ((kind = 'transfer') = (to_reservation_id IS NOT NULL)),
    CHECK ((kind = 'close') = (status IS NOT NULL)),
    CHECK (usd_e6_overage <= usd_e6_reattributed)
);

CREATE UNIQUE INDEX run_payer_events_one_transfer_in
    ON run_payer_events(run_id, to_reservation_id) WHERE kind = 'transfer';

CREATE TRIGGER run_payer_events_no_update
BEFORE UPDATE ON run_payer_events
BEGIN
    SELECT RAISE(ABORT, 'run_payer_events is append-only');
END;

CREATE TRIGGER run_payer_events_no_delete
BEFORE DELETE ON run_payer_events
BEGIN
    SELECT RAISE(ABORT, 'run_payer_events is append-only');
END;

-- Plan §4.5's audit trail (`actor, action, target, at`), created here because §3.5's payer
-- transfer is its first writer: a re-attribution the new payer's month cap cannot cover.
CREATE TABLE admin_audit (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    at REAL NOT NULL
);

CREATE TRIGGER admin_audit_no_update
BEFORE UPDATE ON admin_audit
BEGIN
    SELECT RAISE(ABORT, 'admin_audit is append-only');
END;

CREATE TRIGGER admin_audit_no_delete
BEFORE DELETE ON admin_audit
BEGIN
    SELECT RAISE(ABORT, 'admin_audit is append-only');
END;
