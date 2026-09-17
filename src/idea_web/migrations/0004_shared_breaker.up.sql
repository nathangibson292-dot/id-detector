-- The service-wide Shazam breaker's own state (plan §2.3.5, cycle 4b-ii).
--
-- The policy's two *measurements* are counted from `provider_attempt_events`, which is append-only
-- and shared by every worker process: rule (a)'s denominator is the resolved Shazam attempts on one
-- egress in the rolling window, and rule (b)'s daily budget is the **dispatched** attempts on that
-- egress today -- a prepared attempt that was never sent spends no budget (§2.3.3). Neither needs a
-- counter here, and a counter would be a second, divergent truth.
--
-- What a count cannot derive is kept here instead: when an (a) open expires, how many (a) opens
-- this UTC day has already seen (rule (c) latches at three), whether the provider is latched off
-- until an operator re-enables it, and which re-enable the operator has performed. One row per
-- (provider, egress), so a second egress is a row rather than a schema change.
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
    -- The newest resolved event this egress has already been judged on. Re-tripping requires a
    -- NEWER one: without it, one incident's samples stay inside the rolling window and every
    -- judgement after the cooldown lapses opens the breaker again, latching it off after a single
    -- burst. The per-process breaker had the same guarantee, because only a fresh `resolved()`
    -- could move it.
    cursor_event_id INTEGER NOT NULL DEFAULT 0 CHECK (cursor_event_id >= 0),
    updated_at REAL NOT NULL,
    PRIMARY KEY (provider, egress_id)
);
