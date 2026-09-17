"""The service-wide Shazam breaker (plan §2.3.5), shared by every worker process.

1b-iii gave one *process* the policy: a deque of recent outcomes, a daily counter and a latch, all
in memory. A hosted service runs more than one process over one egress, and §2.3.5's denominator is
"all resolved Shazam attempts on that egress" — not one process's view of them. This breaker keeps
the same rules and the same refusal strings, and measures them over the append-only
``provider_attempt_events`` ledger instead:

* **(a)** failure rate over the rolling ``window_seconds`` (minimum sample 20) → open for
  ``cooldown_seconds``;
* **(b)** ``shazam_daily_budget_per_egress`` **dispatched** requests on that egress in one UTC day →
  open for the rest of the day;
* **(c)** three (a)-opens in one UTC day → latched off until an operator re-enables.

Rule (b) counts ``dispatched``, never ``prepared``: the attempt contract is explicit that a prepared
event without a dispatched one was never sent, so a cancelled job or a refused secondary must not
spend the egress's daily budget.

**Re-enabling really re-enables.** The process breaker cleared its sample deque; a ledger cannot be
cleared, so a re-enable records a **cutoff** instead and rule (a) ignores every sample at or before
it. Without that, the failures that caused the trip are still inside the five-minute window and the
very next judgement re-opens the breaker an operator just turned back on.

Only what a count cannot derive is stored (``provider_breaker_state``): the open deadline, the day's
open count, the latch, the re-enable generation and that cutoff. Every change to it is decided
**inside** one write transaction that re-reads the row, so two workers tripping at once cannot lose
a trip. Reading is separable: :meth:`view_with` judges on a caller-supplied connection and writes
nothing, which is what lets the operations view be strictly read-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from id_detector.shazam_breaker import (
    FAILURES,
    BreakerConfig,
    ShazamBlocked,
    ShazamBreaker,
    shazam_off,
)
from idea_web.database import Database

#: The provider whose policy this is; the table is keyed by provider so a second one can be added.
SHAZAM = "shazam"
#: The ledger state that proves a request really left this egress (§2.3.3).
_DISPATCHED = "dispatched"
_RESOLVED = "resolved"
_COLUMNS = "day, opens, open_until, latched, reenable_generation, reenabled_at, cursor_event_id"


def _moment(text: object) -> float | None:
    """One ledger row's ``at`` as epoch seconds, or ``None`` when it cannot be read."""

    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class BreakerState:
    """The durable half of the policy: what counting cannot derive."""

    day: str
    opens: int = 0
    open_until: float | None = None
    latched: bool = False
    reenable_generation: int = 0
    #: Samples at or before this moment are not rule (a)'s business any more.
    reenabled_at: float | None = None
    #: The newest resolved event already judged. A trip needs a newer one than this.
    cursor_event_id: int = 0


class SharedShazamBreaker(ShazamBreaker):
    """§2.3.5 over the shared ledger. A drop-in for the process breaker the pipeline expects."""

    def __init__(
        self,
        database: Database,
        *,
        provider: str = SHAZAM,
        egress_id: str = "default",
        config: BreakerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Deliberately no ShazamBreaker.__init__: this object holds no sample deque, no daily
        # counter and no latch of its own. Every one of them is shared state.
        self.database = database
        self.provider = provider
        self.egress_id = egress_id
        self._config = config or BreakerConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        #: Requests this process has actually sent today, for the moment between the request
        #: leaving and its ``dispatched`` row being visible. Never counts an admission that did
        #: not become a request.
        self._sent_day: str | None = None
        self._sent = 0

    @property
    def config(self) -> BreakerConfig:  # type: ignore[override]
        return self._config

    def configure(self, config: BreakerConfig) -> None:
        """Adopt a new policy. A raised ``reenable_generation`` is applied against the *persisted*
        generation at the next judgement, so it survives a restart."""

        self._config = config

    # -- the ledger -----------------------------------------------------------------------------
    def _now(self, now: datetime | None = None) -> datetime:
        return (now or self.clock()).astimezone(UTC)

    def _sent_today(self, today: str) -> int:
        return self._sent if self._sent_day == today else 0

    @staticmethod
    def _state_of(row: Any, today: str) -> BreakerState:
        if row is None:
            return BreakerState(day=today)
        return BreakerState(
            day=str(row["day"]),
            opens=int(row["opens"]),
            open_until=None if row["open_until"] is None else float(row["open_until"]),
            latched=bool(row["latched"]),
            reenable_generation=int(row["reenable_generation"]),
            reenabled_at=None if row["reenabled_at"] is None else float(row["reenabled_at"]),
            cursor_event_id=int(row["cursor_event_id"]),
        )

    def _read_state(self, connection: Any, today: str) -> BreakerState:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM provider_breaker_state WHERE provider=? AND egress_id=?",
            (self.provider, self.egress_id),
        ).fetchone()
        return self._state_of(row, today)

    def _cutoff(self, state: BreakerState, moment: datetime) -> float | None:
        """The moment rule (a) starts counting from.

        A configured generation ahead of the persisted one is an operator re-enable that has not
        been applied yet; it takes effect now, so now is the cutoff.
        """

        if self._config.reenable_generation > state.reenable_generation:
            return moment.timestamp()
        return state.reenabled_at

    def _window_failures(
        self, connection: Any, now: datetime, since: float | None
    ) -> tuple[int, int, int]:
        """(resolved attempts, qualifying failures, newest event id) in the rolling window."""

        cutoff = now - timedelta(seconds=self._config.window_seconds)
        # The index is on (provider, egress_id, at) and ``at`` is ISO-8601 UTC, so the date prefix
        # bounds the scan; the exact cutoff is applied on the parsed value, never on string order.
        rows = connection.execute(
            "SELECT event_id, outcome, at FROM provider_attempt_events "
            "WHERE provider=? AND egress_id=? AND state=? AND substr(at, 1, 10) >= ?",
            (self.provider, self.egress_id, _RESOLVED, cutoff.date().isoformat()),
        ).fetchall()
        count = failures = newest = 0
        floor = cutoff.timestamp()
        for row in rows:
            at = _moment(row["at"])
            if at is None or at < floor:
                continue
            if since is not None and at <= since:
                continue  # before the operator re-enabled: not this period's evidence
            count += 1
            failures += row["outcome"] in FAILURES
            newest = max(newest, int(row["event_id"]))
        return count, failures, newest

    def _dispatched_today(self, connection: Any, today: str) -> int:
        """Requests that really left this egress today (§2.3.3's ``dispatched``)."""

        row = connection.execute(
            "SELECT COUNT(*) AS used FROM provider_attempt_events "
            "WHERE provider=? AND egress_id=? AND state=? AND substr(at, 1, 10) = ?",
            (self.provider, self.egress_id, _DISPATCHED, today),
        ).fetchone()
        return int(row["used"])

    def _measure(
        self, connection: Any, moment: datetime, today: str, since: float | None
    ) -> tuple[int, int, int, int]:
        count, failures, newest = self._window_failures(connection, moment, since)
        used = max(self._dispatched_today(connection, today), self._sent_today(today))
        return count, failures, used, newest

    def _judge(
        self,
        state: BreakerState,
        *,
        count: int,
        failures: int,
        used: int,
        newest: int,
        stamp: float,
        today: str,
    ) -> tuple[str | None, BreakerState]:
        """Pure: the refusal, and the state that ought to be persisted. No I/O, so it can be
        replayed inside a write transaction against a freshly read row."""

        config = self._config
        # An operator's re-enable is recorded as a generation. Comparing the configured one with
        # the *persisted* one is what makes a bump survive a restart, and the cutoff is what stops
        # the failures that caused the trip from immediately undoing it.
        if config.reenable_generation > state.reenable_generation:
            state = replace(
                state,
                latched=False,
                opens=0,
                open_until=None,
                reenable_generation=config.reenable_generation,
                reenabled_at=stamp,
            )
        if state.day != today:
            # A new UTC day resets rule (c)'s count of opens, never the latch: a latched provider
            # stays off until an operator re-enables it (D8).
            state = replace(state, day=today, opens=0)
        open_now = state.open_until is not None and stamp < state.open_until
        tripping = (
            count >= config.minimum_sample
            and failures * 10_000 > count * config.failure_rate_e4
            # One incident must not trip twice. Its samples stay inside the rolling window long
            # after the cooldown lapses, so a trip needs a resolved event NEWER than the one this
            # egress was last judged on — the per-process breaker's guarantee, which only a fresh
            # ``resolved()`` could satisfy.
            and newest > state.cursor_event_id
        )
        # An open cooldown is not re-tripped; the next period is judged once it lapses. A latched
        # provider is never re-judged at all.
        if tripping and not state.latched and not open_now:
            opens = state.opens + 1
            state = replace(
                state,
                opens=opens,
                open_until=stamp + config.cooldown_seconds,
                latched=opens >= config.latch_count,
                cursor_event_id=newest,
            )
            open_now = True
        if state.latched:
            return "shazam_breaker:c_latch", state
        if used >= config.shazam_daily_budget_per_egress:
            return "shazam_breaker:b_daily_budget", state
        if open_now:
            return "shazam_breaker:a_failure_rate", state
        return None, state

    def _store(self, connection: Any, state: BreakerState, *, now: float) -> None:
        connection.execute(
            "INSERT INTO provider_breaker_state(provider, egress_id, day, opens, open_until, "
            "latched, reenable_generation, reenabled_at, cursor_event_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, egress_id) DO UPDATE SET day=excluded.day, "
            "opens=excluded.opens, open_until=excluded.open_until, latched=excluded.latched, "
            "reenable_generation=excluded.reenable_generation, "
            "reenabled_at=excluded.reenabled_at, cursor_event_id=excluded.cursor_event_id, "
            "updated_at=excluded.updated_at",
            (
                self.provider,
                self.egress_id,
                state.day,
                state.opens,
                state.open_until,
                int(state.latched),
                state.reenable_generation,
                state.reenabled_at,
                state.cursor_event_id,
                now,
            ),
        )

    def view_with(
        self, connection: Any, now: datetime | None = None
    ) -> tuple[str | None, BreakerState]:
        """Judge on a caller-supplied connection, writing nothing.

        The operations view passes a strictly read-only connection here: looking at the breaker
        must never be what opens it, nor what creates or upgrades a database file.
        """

        moment = self._now(now)
        today = moment.date().isoformat()
        state = self._read_state(connection, today)
        count, failures, used, newest = self._measure(
            connection, moment, today, self._cutoff(state, moment)
        )
        return self._judge(
            state,
            count=count,
            failures=failures,
            used=used,
            newest=newest,
            stamp=moment.timestamp(),
            today=today,
        )

    def view(self, now: datetime | None = None) -> tuple[str | None, BreakerState]:
        """What the breaker refuses right now, **without writing anything**."""

        with self.database.read() as connection:
            return self.view_with(connection, now)

    def refusal(self, now: datetime | None = None) -> str | None:
        """Judge against the ledger and persist any change, atomically across processes."""

        moment = self._now(now)
        today = moment.date().isoformat()
        stamp = moment.timestamp()
        reason, desired = self.view(moment)
        with self.database.read() as connection:
            current = self._read_state(connection, today)
        if desired == current:
            return reason
        # Something must change (a trip, a day roll, an operator's re-enable). Decide it again
        # inside ONE write transaction against a freshly read row: SQLite serialises writers, so a
        # trip another process committed in the meantime is seen here instead of being overwritten.
        with self.database.write() as connection:
            state = self._read_state(connection, today)
            count, failures, used, newest = self._measure(
                connection, moment, today, self._cutoff(state, moment)
            )
            reason, desired = self._judge(
                state,
                count=count,
                failures=failures,
                used=used,
                newest=newest,
                stamp=stamp,
                today=today,
            )
            if desired != state:
                self._store(connection, desired, now=stamp)
        return reason

    # -- the ShazamBreaker seam ------------------------------------------------------------------
    def reason(self) -> str | None:
        return self.refusal()

    def reenable(self) -> None:
        """Explicit operator action: clear the latch and the day's opens, keep the daily budget.

        The cutoff is the point of it. The ledger is append-only, so the failures that tripped the
        breaker cannot be deleted the way the process breaker emptied its deque; recording the
        moment and ignoring everything at or before it has the same effect.
        """

        moment = self._now()
        today = moment.date().isoformat()
        with self.database.write() as connection:
            state = self._read_state(connection, today)
            self._store(
                connection,
                BreakerState(
                    day=today,
                    opens=0,
                    open_until=None,
                    latched=False,
                    reenable_generation=max(
                        state.reenable_generation + 1, self._config.reenable_generation
                    ),
                    reenabled_at=moment.timestamp(),
                ),
                now=moment.timestamp(),
            )

    def dispatch(self, *, running_free: bool) -> date:
        if shazam_off():
            raise ShazamBlocked("shazam_manual_off")
        reason = self.refusal()
        if reason and not running_free:
            raise ShazamBlocked(reason)
        # Admission is not a request. Nothing is counted here: the budget moves in ``sent()``,
        # when the request is about to enter network I/O.
        return self._now().date()

    def release_dispatch(self, dispatch_day: date) -> None:
        """A no-op: an admission that never became a request was never counted at all."""

        del dispatch_day

    def sent(self) -> None:
        """The admitted request is entering network I/O — the moment it counts (§2.3.3).

        The durable count is the ledger's ``dispatched`` row, written by the attempt journal; this
        keeps one process's own budget exact in the moment before that row is visible.
        """

        today = self._now().date().isoformat()
        if self._sent_day != today:
            self._sent_day, self._sent = today, 0
        self._sent += 1

    def resolved(self, outcome: str) -> None:
        """Note an outcome. Judging deliberately happens at the *next* admission.

        The caller writes the resolved event after this returns, so judging here would read the
        ledger without it. The next ``dispatch()``/``reason()`` — which is what any further request
        must pass — sees the committed row, so nothing escapes on a stale reading.
        """

        del outcome
