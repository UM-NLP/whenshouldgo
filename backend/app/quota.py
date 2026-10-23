from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import Lock

from .domain import Principal
from .entitlements import Allowance, PolicyRegistry, default_registry


@dataclass
class Usage:
    successful_turns: int = 0
    activated_trips: int = 0
    committed_keys: set[str] = field(default_factory=set)
    reserved_keys: set[str] = field(default_factory=set)


# Upper bound on ledger records held in this in-memory development service. When it is exceeded the
# OLDEST EMPTY records (no committed turns, no trips, no keys) are dropped; a record with real usage
# is never evicted (that would hand back allowance). Durable, shared ledgers and identity-creation
# limits are tracked under ACCESS-02 (#35).
MAX_LEDGER_RECORDS = 100_000


class ReservationConflict(Exception):
    """The same idempotency key already has an in-flight reservation."""


@dataclass(frozen=True)
class TurnReservation:
    uid: str
    idempotency_key: str
    # True when the key was already committed: a replay that holds no allowance.
    duplicate: bool = False


class QuotaService:
    """In-memory development implementation; production maps this contract to Firestore.

    Limits come from the injected versioned PolicyRegistry, never from constants here.
    ``reserve`` / ``commit`` / ``release`` are atomic under one lock: a reservation
    holds one unit of allowance until the turn completes (commit) or fails (release),
    so concurrent requests can never overspend and a denial happens before any
    workspace mutation.

    TODO(#22): enforce the ROLLING_30_DAYS window for registered-free principals and
    back this contract with Firestore (UsageReservation).
    """

    def __init__(self, policies: PolicyRegistry | None = None) -> None:
        self.policies = policies or default_registry()
        self._usage: dict[str, Usage] = {}
        self._lock = Lock()

    def _record(self, uid: str) -> Usage:
        """Get or create the record (caller holds the lock); bounded, see MAX_LEDGER_RECORDS."""
        usage = self._usage.get(uid)
        if usage is None:
            usage = self._usage[uid] = Usage()
            if len(self._usage) > MAX_LEDGER_RECORDS:
                empty = [k for k, u in self._usage.items() if k != uid and not (
                    u.successful_turns or u.activated_trips or u.committed_keys or u.reserved_keys)]
                for key in empty[: len(self._usage) - MAX_LEDGER_RECORDS]:
                    del self._usage[key]
        return usage

    def usage_for(self, uid: str) -> Usage:
        """Return a snapshot; callers cannot mutate the ledger through it."""
        with self._lock:
            usage = self._usage.get(uid) or Usage()
            return replace(
                usage,
                committed_keys=set(usage.committed_keys),
                reserved_keys=set(usage.reserved_keys),
            )

    def ensure_record(self, uid: str) -> None:
        """Idempotently create the principal's ledger record (session bootstrap)."""
        with self._lock:
            self._record(uid)

    def _allowance(self, principal: Principal) -> Allowance | None:
        return self.policies.for_kind(principal.kind, principal.policy_version)

    @staticmethod
    def _has_room(usage: Usage, allowance: Allowance | None) -> bool:
        if allowance is None:
            return False
        return (
            usage.successful_turns + len(usage.reserved_keys) < allowance.user_turn_limit
            and usage.activated_trips < allowance.activated_trip_limit
        )

    def allowed(self, principal: Principal) -> bool:
        """Advisory check for UX gating. Enforcement happens in ``reserve``."""
        with self._lock:
            usage = self._record(principal.uid)
            return self._has_room(usage, self._allowance(principal))

    def reserve(self, principal: Principal, idempotency_key: str) -> TurnReservation:
        """Atomically check and hold one turn. Raises PermissionError when exhausted."""
        with self._lock:
            usage = self._record(principal.uid)
            if idempotency_key in usage.committed_keys:
                return TurnReservation(principal.uid, idempotency_key, duplicate=True)
            if idempotency_key in usage.reserved_keys:
                raise ReservationConflict("request with this idempotency key is in flight")
            if not self._has_room(usage, self._allowance(principal)):
                raise PermissionError("allowance exhausted")
            usage.reserved_keys.add(idempotency_key)
            return TurnReservation(principal.uid, idempotency_key)

    def commit(self, reservation: TurnReservation) -> Usage:
        with self._lock:
            usage = self._record(reservation.uid)
            if reservation.duplicate or reservation.idempotency_key in usage.committed_keys:
                usage.reserved_keys.discard(reservation.idempotency_key)
                return usage
            if reservation.idempotency_key not in usage.reserved_keys:
                raise RuntimeError("commit without an active reservation")
            usage.reserved_keys.discard(reservation.idempotency_key)
            usage.successful_turns += 1
            usage.committed_keys.add(reservation.idempotency_key)
            return usage

    def release(self, reservation: TurnReservation) -> None:
        with self._lock:
            usage = self._usage.get(reservation.uid)
            if usage is not None and not reservation.duplicate:
                usage.reserved_keys.discard(reservation.idempotency_key)

    def commit_turn(self, principal: Principal, idempotency_key: str) -> Usage:
        """Check and count one turn atomically (for callers with no work in between)."""
        with self._lock:
            usage = self._record(principal.uid)
            if idempotency_key in usage.committed_keys:
                return usage
            if not self._has_room(usage, self._allowance(principal)):
                raise PermissionError("allowance exhausted")
            usage.successful_turns += 1
            usage.committed_keys.add(idempotency_key)
            return usage
