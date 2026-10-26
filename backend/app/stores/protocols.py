"""Durable-state protocols (ACCESS-02 / #35, decisions 18, 19, 26, 27).

Every store keeps the MINIMUM transactional operation the caller needs, so one implementation
(memory, Firestore) can make it atomic. Callers never read-modify-write across two calls.

What the stores hold, and what they must never hold:

* ids, enums, counts, timestamps, hashes and user-typed data only;
* NEVER a coordinate, a raw provider response, route geometry or Maps content, except the
  ``DisplayCacheStore`` (Google's grounded answer, redisplay only, 30 days at most, decision 18);
* every persisted document carries ``schema_version`` and ``kind``
  (docs/data-and-history-design.md); the in-memory stores hold typed objects that carry them.

Time is always passed in (``now``): the stores never read a wall clock, so tests and the server
agree on one clock, and expiry is decided from server time, not from client input.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..discovery.cache import DisplayCacheStore
from ..domain import ConsentRecord, TripWorkspace

T = TypeVar("T")

# A turn reservation that is neither committed nor released within this long is abandoned (the
# process died). Placeholder: above the 25 s tenant-lock wait and the 20 s run deadline.
RESERVATION_TTL = timedelta(minutes=2)
# A stored-but-uncommitted replay record is abandoned after the same time.
PENDING_REPLAY_TTL = RESERVATION_TTL
# How long a committed replay record is kept (owner-tunable placeholder). A replay after this
# finds the quota key still committed and gets a typed RESULT_EXPIRED, never a second charge.
REPLAY_RETENTION = timedelta(days=7)
# Display cache hard cap (decision 18); mirrors ``domain.MAX_MAPS_CACHE``.
MAX_CACHE_DAYS = 30


# --- quota ---------------------------------------------------------------------------------------
@dataclass
class Usage:
    """One principal's ledger record. A snapshot handed to callers is a copy."""

    successful_turns: int = 0
    activated_trips: int = 0
    committed_keys: set[str] = field(default_factory=set)
    reserved_keys: set[str] = field(default_factory=set)
    # key -> when it was reserved (server time); a key missing here never expires (test fixtures).
    reserved_at: dict[str, datetime] = field(default_factory=dict)
    # Workspaces whose trip was already counted in this window: activation counts once each.
    activated_workspaces: set[str] = field(default_factory=set)
    # Start of the current rolling window; None for a LIFETIME allowance.
    window_started_at: datetime | None = None


@dataclass(frozen=True)
class QuotaLimits:
    """The allowance for one principal, resolved by the server from the versioned policy."""

    turn_limit: int
    trip_limit: int
    window: timedelta | None  # None: lifetime; 30 days for the registered rolling window


class ReserveOutcome(StrEnum):
    RESERVED = "RESERVED"
    DUPLICATE = "DUPLICATE"  # the key was already committed: a replay that holds no allowance
    IN_FLIGHT = "IN_FLIGHT"  # the key has a live reservation
    EXHAUSTED = "EXHAUSTED"


@runtime_checkable
class QuotaStore(Protocol):
    """Atomic per-principal ledger. Each method is one transaction."""

    def ensure(self, uid: str, now: datetime, window: timedelta | None) -> None: ...
    def snapshot(self, uid: str, now: datetime, window: timedelta | None) -> Usage: ...
    def has_room(self, uid: str, limits: QuotaLimits, now: datetime) -> bool: ...
    def reserve(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> ReserveOutcome: ...
    def commit(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage: ...
    def release(self, uid: str, key: str) -> None: ...
    def commit_turn(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage: ...
    def activate_trip(
        self, uid: str, workspace_id: str, limits: QuotaLimits, now: datetime) -> bool: ...


# --- consent -------------------------------------------------------------------------------------
@runtime_checkable
class ConsentLedgerStore(Protocol):
    """Append-only ledger primitive. ``update`` runs ``fn`` against the owner's current entries and
    appends the records it returns, atomically; there is no way to change or remove an entry.
    ``fn`` may run more than once (a retried transaction), so it must be pure."""

    def read(self, owner_id: str) -> tuple[ConsentRecord, ...]: ...
    def update(
        self, owner_id: str,
        fn: Callable[[tuple[ConsentRecord, ...]], tuple[tuple[ConsentRecord, ...], T]],
    ) -> T: ...


# --- workspace -----------------------------------------------------------------------------------
@runtime_checkable
class WorkspaceStore(Protocol):
    """Tenant-keyed ``TripWorkspace`` (``name`` is "default" in Phase 0). ``ttl`` makes a write
    refresh a server-time expiry (guest: 7 inactive days); an expired workspace reads as absent
    whether or not the TTL sweeper has deleted it yet."""

    def get(self, owner_id: str, name: str, now: datetime) -> TripWorkspace | None: ...
    def put(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
            ttl: timedelta | None) -> None: ...
    def put_if_version(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
                       ttl: timedelta | None, *, expected_version: int | None) -> bool:
        """Compare-and-set: write only when the live stored workspace has ``expected_version``
        (None: there is no live workspace). True when written, False when another writer got there
        first, so two instances cannot lose each other's update. Atomic in every adapter."""
        ...
    def delete(self, owner_id: str, name: str) -> None: ...
    def delete_if_version(self, owner_id: str, name: str, *, expected_version: int) -> bool:
        """Delete only when the live stored workspace has ``expected_version`` (the version the
        caller itself wrote): a later writer's workspace is never removed. Atomic in every
        adapter. True when deleted."""
        ...


# --- idempotency / replay --------------------------------------------------------------------------
@dataclass(frozen=True)
class StoredReplay:
    fingerprint: str
    record: Any  # orchestrator.ReplayRecord (opaque to the store; Firestore uses a codec)
    pending: bool


@runtime_checkable
class IdempotencyStore(Protocol):
    """``begin`` stores a record as pending (invisible to replay), ``commit`` makes it a replay
    source, ``rollback`` removes it. A pending record that is never committed expires."""

    def get(self, owner_id: str, key: str, now: datetime) -> StoredReplay | None: ...
    def begin(self, owner_id: str, key: str, fingerprint: str, record: Any, now: datetime) -> None: ...
    def commit(self, owner_id: str, key: str, now: datetime) -> None: ...
    def rollback(self, owner_id: str, key: str) -> None: ...


# --- cost: whole-pilot spend counter and kill switch ---------------------------------------------
class SpendBasis(StrEnum):
    """What the pilot cap counts. ``ALL`` (the default) is every recorded cost entry: a cap that
    counted only provider-attributed failures would never trip under healthy traffic. The other
    basis is decision 23's literal reading and exists so the owner can pick it deliberately."""

    ALL = "ALL"
    PROVIDER_ATTRIBUTED = "PROVIDER_ATTRIBUTED"


@dataclass(frozen=True)
class SpendBudget:
    max_tokens: int | None = None  # input + output tokens over the whole pilot
    max_usd: Decimal | None = None  # priced spend only (a failed call has no price)
    basis: SpendBasis = SpendBasis.ALL

    def __post_init__(self) -> None:
        if self.max_tokens is None and self.max_usd is None:
            raise ValueError("a spend budget needs a token or a dollar cap")
        if (self.max_tokens is not None and self.max_tokens < 1) or (
                self.max_usd is not None and self.max_usd <= 0):
            raise ValueError("a spend cap must be positive")

    @property
    def max_usd_micros(self) -> int | None:
        return None if self.max_usd is None else int(self.max_usd * 1_000_000)


@dataclass(frozen=True)
class CostEntryDelta:
    input_tokens: int
    output_tokens: int
    usd_micros: int  # 0 when unpriced
    provider_attributed: bool


@dataclass(frozen=True)
class SpendTotals:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_usd_micros: int = 0
    provider_input_tokens: int = 0
    provider_output_tokens: int = 0
    provider_usd_micros: int = 0
    over_budget: bool = False  # as of the budget the last ``add_spend`` call evaluated


class KillSwitchReason(StrEnum):
    SPEND_CAP = "SPEND_CAP"
    DISCOVERY = "DISCOVERY"  # the discovery client tripped it (repeated access denied)
    PLACES = "PLACES"  # the Places/Routes service tripped it (repeated access denied)
    OPERATOR = "OPERATOR"


# A kill switch has one state per provider path. "global" is the whole-pilot switch (the spend cap
# trips it; an operator may): it stops every paid path. A provider path ("discovery", "places")
# stops only that path, so a Places failure never switches discovery off, and the other way round.
KILL_PATH_GLOBAL = "global"
KILL_PATH_DISCOVERY = "discovery"
KILL_PATH_PLACES = "places"
KILL_PATHS = (KILL_PATH_GLOBAL, KILL_PATH_DISCOVERY, KILL_PATH_PLACES)


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool = False
    reason: KillSwitchReason | None = None
    tripped_at: datetime | None = None


@runtime_checkable
class CostStore(Protocol):
    def add_spend(
        self, delta: CostEntryDelta, budget: SpendBudget | None, now: datetime
    ) -> SpendTotals:
        """Add to the counters and, in the SAME transaction, trip the kill switch when ``budget``
        is reached."""
        ...

    def totals(self) -> SpendTotals: ...

    def kill_switch(self, path: str = KILL_PATH_GLOBAL) -> KillSwitchState: ...

    def trip_kill_switch(self, reason: KillSwitchReason, now: datetime,
                         path: str = KILL_PATH_GLOBAL) -> bool:
        """True when this call tripped it (False: already tripped; the first reason stays)."""
        ...

    def reset_kill_switch(self, now: datetime, path: str = KILL_PATH_GLOBAL) -> None: ...


def known_kill_path(path: str) -> str:
    """A kill-switch path must be one of ``KILL_PATHS`` (never free text: it becomes a document id)."""
    if path not in KILL_PATHS:
        raise ValueError("unknown kill switch path")
    return path


def over_budget(totals: SpendTotals, budget: SpendBudget | None) -> bool:
    """Whether the counters have reached ``budget`` (shared by every implementation)."""
    if budget is None:
        return False
    if budget.basis == SpendBasis.ALL:
        tokens = totals.total_input_tokens + totals.total_output_tokens
        usd = totals.total_usd_micros
    else:
        tokens = totals.provider_input_tokens + totals.provider_output_tokens
        usd = totals.provider_usd_micros
    if budget.max_tokens is not None and tokens >= budget.max_tokens:
        return True
    cap = budget.max_usd_micros
    return cap is not None and usd >= cap


__all__ = [
    "MAX_CACHE_DAYS", "PENDING_REPLAY_TTL", "REPLAY_RETENTION", "RESERVATION_TTL",
    "ConsentLedgerStore", "CostEntryDelta", "CostStore", "DisplayCacheStore", "IdempotencyStore",
    "KILL_PATHS", "KILL_PATH_DISCOVERY", "KILL_PATH_GLOBAL", "KILL_PATH_PLACES",
    "KillSwitchReason", "KillSwitchState", "QuotaLimits", "QuotaStore", "ReserveOutcome",
    "SpendBasis", "SpendBudget", "SpendTotals", "StoredReplay", "Usage", "WorkspaceStore",
    "known_kill_path", "over_budget",
]
