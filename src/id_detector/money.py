"""Exact-integer USD reservation, dispatch admission, refund, and settlement."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from id_detector.contracts import ProviderOutcome

BILLABLE_OUTCOMES = frozenset({"match", "no_match", "timeout_post", "http_5xx", "malformed"})
ZERO_COST_OUTCOMES = frozenset(
    {
        "connect_error",
        "timeout_pre",
        "http_429",
        "http_503",
        "auth_error",
        "quota_error",
    }
)
#: Plan §2.3.3 terminal-provider outcomes: cost 0, never retried, and the primary stops at once.
TERMINAL_PROVIDER_OUTCOMES = frozenset({"auth_error", "quota_error"})
#: Pre-receipt transport failures — the provider could not be reached at all (cost 0).
UNREACHABLE_OUTCOMES = frozenset({"connect_error", "timeout_pre"})


class BudgetExhausted(RuntimeError):
    """Raised before provider I/O when the requested reservation exceeds its cap."""


class ReservationExhausted(RuntimeError):
    """Raised immediately before a dispatch that the hard reservation cannot admit."""


@dataclass(frozen=True)
class UsdReservation:
    planned: int
    unit_usd_e6: int
    usd_e6_reserved: int
    usd_e2_reserved: int
    effective_cap_e2: int


@dataclass(frozen=True)
class UsdSettlement:
    usd_e6_reserved: int
    usd_e6_spent: int
    usd_e2_reserved: int
    usd_e2_spent: int
    usd_e6_released: int


def ceil_e2(usd_e6: int) -> int:
    if usd_e6 < 0:
        raise ValueError("usd_e6 must be non-negative")
    return (usd_e6 + 9_999) // 10_000


def reserve_usd(
    *,
    planned: int,
    unit_usd_e6: int,
    recipe_max_usd_e2: int,
    configured_max_usd_e2: int | None,
) -> UsdReservation:
    """Reserve 105% of planned request units, refusing above the effective local cap."""

    if planned < 0:
        raise ValueError("planned must be non-negative")
    if unit_usd_e6 <= 0:
        raise ValueError("unit_usd_e6 must be positive")
    if recipe_max_usd_e2 < 0:
        raise ValueError("recipe_max_usd_e2 must be non-negative")
    if configured_max_usd_e2 is not None and configured_max_usd_e2 < 0:
        raise ValueError("configured_max_usd_e2 must be non-negative")
    effective_cap = recipe_max_usd_e2
    if configured_max_usd_e2 is not None:
        effective_cap = min(effective_cap, configured_max_usd_e2)
    base = planned * unit_usd_e6
    reserved_e6 = (base * 105 + 99) // 100
    reserved_e2 = ceil_e2(reserved_e6)
    reservation = UsdReservation(
        planned=planned,
        unit_usd_e6=unit_usd_e6,
        usd_e6_reserved=reserved_e6,
        usd_e2_reserved=reserved_e2,
        effective_cap_e2=effective_cap,
    )
    if reserved_e2 > effective_cap:
        raise BudgetExhausted(
            f"USD reservation {reserved_e2} cents exceeds effective cap {effective_cap} cents"
        )
    return reservation


class UsdAdmitter:
    """Thread-safe in-process hard cap for one run's AudD dispatches."""

    def __init__(self, reservation: UsdReservation) -> None:
        self.reservation = reservation
        self._remaining = reservation.usd_e6_reserved
        self._spent = 0
        self._outstanding = 0
        self._admitted = 0
        self._refunded = 0
        self._settled: UsdSettlement | None = None
        self._lock = Lock()

    @property
    def usd_e6_remaining(self) -> int:
        with self._lock:
            return self._remaining

    @property
    def usd_e6_spent(self) -> int:
        with self._lock:
            return self._spent

    @property
    def admitted_units(self) -> int:
        with self._lock:
            return self._admitted

    @property
    def refunded_units(self) -> int:
        with self._lock:
            return self._refunded

    def admit(self) -> None:
        """Atomically consume one request unit immediately before provider dispatch."""

        with self._lock:
            if self._settled is not None:
                raise RuntimeError("USD reservation is already settled")
            unit = self.reservation.unit_usd_e6
            if self._remaining < unit:
                raise ReservationExhausted("USD reservation has no complete request unit left")
            self._remaining -= unit
            self._outstanding += 1
            self._admitted += 1

    def resolve(self, outcome: ProviderOutcome) -> None:
        """Settle one admitted unit; zero-cost outcomes return it to the hard cap."""

        if outcome not in BILLABLE_OUTCOMES | ZERO_COST_OUTCOMES:
            raise ValueError(f"unknown provider outcome: {outcome}")
        with self._lock:
            if self._settled is not None:
                raise RuntimeError("USD reservation is already settled")
            if self._outstanding <= 0:
                raise RuntimeError("no admitted USD unit is awaiting resolution")
            self._outstanding -= 1
            if outcome in BILLABLE_OUTCOMES:
                self._spent += self.reservation.unit_usd_e6
            else:
                self._remaining += self.reservation.unit_usd_e6
                self._refunded += 1

    def restore_spent(self, spent_usd_e6: int) -> bool:
        """Charge durable spend a previous pass of this run already made, exactly and idempotently.

        The amount is the ledger's own µUSD sum (each attempt at the price its event recorded), so
        a price change between passes can never re-price spent money. Charging to the same figure
        twice is a no-op. ``False`` means the reservation cannot cover what is already spent: the
        remainder is then zero and no further request may be admitted.
        """

        if spent_usd_e6 < 0:
            raise ValueError("spent_usd_e6 must be non-negative")
        with self._lock:
            if self._settled is not None:
                raise RuntimeError("USD reservation is already settled")
            delta = spent_usd_e6 - self._spent
            if delta <= 0:
                return True
            self._spent += delta
            taken = min(delta, self._remaining)
            self._remaining -= taken
            return taken == delta

    def settle(self) -> UsdSettlement:
        """Release the unspent reservation; a dispatched-but-unresolved unit settles as spent.

        A unit admitted for a dispatch that never reported an outcome is ambiguous (plan §2.3.3):
        the request may well have reached the provider, so settlement charges it rather than
        assuming a refund. Settlement is idempotent so that a failing or cancelled run can always
        journal the same terminal figures.
        """

        with self._lock:
            if self._settled is not None:
                return self._settled
            if self._outstanding:
                self._spent += self._outstanding * self.reservation.unit_usd_e6
                self._outstanding = 0
            released = self._remaining
            self._remaining = 0
            self._settled = UsdSettlement(
                usd_e6_reserved=self.reservation.usd_e6_reserved,
                usd_e6_spent=self._spent,
                usd_e2_reserved=self.reservation.usd_e2_reserved,
                usd_e2_spent=ceil_e2(self._spent),
                usd_e6_released=released,
            )
            return self._settled
