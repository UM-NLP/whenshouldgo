from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .domain import Principal
from .entitlements import Allowance, Period, PolicyRegistry, default_registry
from .stores.memory import MAX_LEDGER_RECORDS, MemoryQuotaStore
from .stores.protocols import QuotaLimits, QuotaStore, ReserveOutcome, Usage

# The rolling window of a ``ROLLING_30_DAYS`` allowance (decisions 5 and 27).
ROLLING_WINDOW = timedelta(days=30)

__all__ = [
    "MAX_LEDGER_RECORDS", "QuotaService", "ReservationConflict", "TurnReservation", "Usage",
]


class ReservationConflict(Exception):
    """The same idempotency key already has an in-flight reservation."""


@dataclass(frozen=True)
class TurnReservation:
    uid: str
    idempotency_key: str
    # True when the key was already committed: a replay that holds no allowance.
    duplicate: bool = False


class QuotaService:
    """The quota contract over a ``QuotaStore`` (in-memory by default; Firestore in production).

    Limits come from the injected versioned PolicyRegistry, never from constants here.
    ``reserve`` / ``commit`` / ``release`` are each one atomic store operation: a reservation
    holds one unit of allowance until the turn completes (commit) or fails (release), so
    concurrent requests can never overspend and a denial happens before any workspace mutation.
    An abandoned reservation (the process died) expires, so it cannot hold allowance forever.

    A ``ROLLING_30_DAYS`` allowance (registered free) counts in a window that starts at the first
    use and restarts 30 days later with fresh counts; a ``LIFETIME`` allowance (guest) never
    resets. ``activate_trip`` counts a workspace's activation once.
    """

    def __init__(
        self, policies: PolicyRegistry | None = None, store: QuotaStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policies = policies or default_registry()
        self._store: QuotaStore = (
            store if store is not None else MemoryQuotaStore(max_records=MAX_LEDGER_RECORDS))
        # The in-memory ledger dict (tests and dev tools inspect it); None for a durable store.
        self._usage: dict[str, Usage] | None = getattr(self._store, "usage", None)
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- policy ---------------------------------------------------------------------------------
    def _allowance(self, principal: Principal) -> Allowance | None:
        return self.policies.for_kind(principal.kind, principal.policy_version)

    def window_for(self, principal: Principal) -> timedelta | None:
        allowance = self._allowance(principal)
        return ROLLING_WINDOW if allowance and allowance.period == Period.ROLLING_30_DAYS else None

    def _limits(self, principal: Principal) -> QuotaLimits:
        allowance = self._allowance(principal)
        if allowance is None:  # no entitlement: nothing is ever allowed
            return QuotaLimits(0, 0, None)
        return QuotaLimits(allowance.user_turn_limit, allowance.activated_trip_limit,
                           self.window_for(principal))

    # -- reads ----------------------------------------------------------------------------------
    def usage_for(self, uid: str) -> Usage:
        """Return a snapshot (no window applied); callers cannot mutate the ledger through it."""
        return self._store.snapshot(uid, self._clock(), None)

    def current_usage(self, principal: Principal) -> Usage:
        """The principal's usage in its CURRENT window: a registered window that has run its
        course is restarted (and that is persisted), so ``window_started_at`` is stable."""
        window = self.window_for(principal)
        now = self._clock()
        self._store.ensure(principal.uid, now, window)
        return self._store.snapshot(principal.uid, now, window)

    def resets_at(self, principal: Principal) -> datetime | None:
        """When the current rolling window ends; None for a lifetime allowance."""
        window = self.window_for(principal)
        if window is None:
            return None
        started = self.current_usage(principal).window_started_at
        return started + window if started is not None else None

    def ensure_record(self, principal_or_uid: Principal | str) -> None:
        """Idempotently create the principal's ledger record (session bootstrap)."""
        if isinstance(principal_or_uid, Principal):
            self._store.ensure(principal_or_uid.uid, self._clock(), self.window_for(principal_or_uid))
        else:
            self._store.ensure(principal_or_uid, self._clock(), None)

    def allowed(self, principal: Principal) -> bool:
        """Advisory check for UX gating. Enforcement happens in ``reserve``."""
        return self._store.has_room(principal.uid, self._limits(principal), self._clock())

    # -- turns ----------------------------------------------------------------------------------
    def reserve(self, principal: Principal, idempotency_key: str) -> TurnReservation:
        """Atomically check and hold one turn. Raises PermissionError when exhausted."""
        outcome = self._store.reserve(
            principal.uid, idempotency_key, self._limits(principal), self._clock())
        if outcome == ReserveOutcome.DUPLICATE:
            return TurnReservation(principal.uid, idempotency_key, duplicate=True)
        if outcome == ReserveOutcome.IN_FLIGHT:
            raise ReservationConflict("request with this idempotency key is in flight")
        if outcome == ReserveOutcome.EXHAUSTED:
            raise PermissionError("allowance exhausted")
        return TurnReservation(principal.uid, idempotency_key)

    def commit(self, reservation: TurnReservation) -> Usage:
        if reservation.duplicate:
            return self._store.snapshot(reservation.uid, self._clock(), None)
        # The window rides on the principal; a commit never changes it, so the stored one is kept.
        return self._store.commit(reservation.uid, reservation.idempotency_key,
                                  _KEEP_WINDOW, self._clock())

    def release(self, reservation: TurnReservation) -> None:
        if not reservation.duplicate:
            self._store.release(reservation.uid, reservation.idempotency_key)

    def commit_turn(self, principal: Principal, idempotency_key: str) -> Usage:
        """Check and count one turn atomically (for callers with no work in between)."""
        return self._store.commit_turn(
            principal.uid, idempotency_key, self._limits(principal), self._clock())

    # -- trips ----------------------------------------------------------------------------------
    def activate_trip(self, principal: Principal, workspace_id: str) -> bool:
        """Count a trip activation once per workspace. True when this call counted it, False when
        it was already counted (idempotent). Raises PermissionError at the trip limit."""
        return self._store.activate_trip(
            principal.uid, workspace_id, self._limits(principal), self._clock())


# ``commit`` has no principal, hence no window: the store treats this as "apply no roll".
_KEEP_WINDOW = QuotaLimits(0, 0, None)
