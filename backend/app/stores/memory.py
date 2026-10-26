"""In-memory implementations of the store protocols (development and tests; per process).

These are the pre-existing in-process behaviours moved behind the protocols. State is NOT shared
across processes or Cloud Run instances and is lost on restart: production composes the
Firestore adapters instead (``app/composition.py``). The ``DisplayCacheStore`` implementation is
``app.discovery.cache.DisplayCache``.

Some attributes (``usage``, ``items``) are plain dicts on purpose: existing tests and dev tools
inspect them. They are not part of the protocols.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar

from ..domain import ConsentRecord, TripWorkspace
from . import quota_logic as logic
from .protocols import (
    PENDING_REPLAY_TTL,
    REPLAY_RETENTION,
    CostEntryDelta,
    KillSwitchReason,
    KillSwitchState,
    QuotaLimits,
    ReserveOutcome,
    SpendBudget,
    SpendTotals,
    StoredReplay,
    Usage,
    over_budget,
)

T = TypeVar("T")

# Upper bound on ledger records held in memory. When it is exceeded the OLDEST EMPTY records (no
# committed turns, no trips, no keys) are dropped; a record with real usage is never evicted (that
# would hand back allowance). Durable ledgers are the Firestore adapters.
MAX_LEDGER_RECORDS = 100_000


class MemoryQuotaStore:
    def __init__(self, max_records: int = MAX_LEDGER_RECORDS) -> None:
        self._max_records = max_records
        self.usage: dict[str, Usage] = {}
        self._lock = threading.Lock()

    def _record(self, uid: str) -> Usage:
        usage = self.usage.get(uid)
        if usage is None:
            usage = self.usage[uid] = Usage()
            if len(self.usage) > self._max_records:
                empty = [k for k, u in self.usage.items() if k != uid and not (
                    u.successful_turns or u.activated_trips or u.committed_keys
                    or u.reserved_keys or u.activated_workspaces)]
                for key in empty[: len(self.usage) - self._max_records]:
                    del self.usage[key]
        return usage

    def ensure(self, uid: str, now: datetime, window: timedelta | None) -> None:
        with self._lock:
            logic.prepare(self._record(uid), now, window)

    def snapshot(self, uid: str, now: datetime, window: timedelta | None) -> Usage:
        with self._lock:
            current = self.usage.get(uid)
            copy = logic.copy_usage(current) if current is not None else Usage()
            return logic.prepare(copy, now, window)

    def has_room(self, uid: str, limits: QuotaLimits, now: datetime) -> bool:
        with self._lock:
            current = self.usage.get(uid)
            copy = logic.copy_usage(current) if current is not None else Usage()
            return logic.has_room(logic.prepare(copy, now, limits.window), limits)

    def reserve(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> ReserveOutcome:
        with self._lock:
            usage = logic.prepare(self._record(uid), now, limits.window)
            return logic.reserve(usage, key, limits, now)

    def commit(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage:
        with self._lock:
            usage = logic.prepare(self._record(uid), now, limits.window)
            logic.commit(usage, key)
            return logic.copy_usage(usage)

    def release(self, uid: str, key: str) -> None:
        with self._lock:
            usage = self.usage.get(uid)
            if usage is not None:
                logic.release(usage, key)

    def commit_turn(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage:
        with self._lock:
            usage = logic.prepare(self._record(uid), now, limits.window)
            logic.commit_turn(usage, key, limits)
            return logic.copy_usage(usage)

    def activate_trip(
        self, uid: str, workspace_id: str, limits: QuotaLimits, now: datetime
    ) -> bool:
        with self._lock:
            usage = logic.prepare(self._record(uid), now, limits.window)
            return logic.activate_trip(usage, workspace_id, limits)


class MemoryConsentLedger:
    """Append-only: ``update`` can only add the records its function returns."""

    def __init__(self) -> None:
        self._ledger: dict[str, list[ConsentRecord]] = defaultdict(list)
        self._lock = threading.Lock()

    def read(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        with self._lock:
            return tuple(self._ledger.get(owner_id, ()))

    def update(
        self, owner_id: str,
        fn: Callable[[tuple[ConsentRecord, ...]], tuple[tuple[ConsentRecord, ...], T]],
    ) -> T:
        with self._lock:
            appended, result = fn(tuple(self._ledger.get(owner_id, ())))
            if appended:
                self._ledger[owner_id].extend(appended)
            return result


class MemoryWorkspaceStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], TripWorkspace] = {}
        self._expires: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def get(self, owner_id: str, name: str, now: datetime) -> TripWorkspace | None:
        key = (owner_id, name)
        with self._lock:
            expires = self._expires.get(key)
            if expires is not None and now >= expires:
                self.items.pop(key, None)
                del self._expires[key]
                return None
            return self.items.get(key)

    def put(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
            ttl: timedelta | None) -> None:
        if workspace.owner_id != owner_id:
            raise ValueError("workspace owner does not match the tenant key")
        key = (owner_id, name)
        with self._lock:
            self.items[key] = workspace
            if ttl is None:
                self._expires.pop(key, None)
            else:
                self._expires[key] = now + ttl

    def put_if_version(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
                       ttl: timedelta | None, *, expected_version: int | None) -> bool:
        if workspace.owner_id != owner_id:
            raise ValueError("workspace owner does not match the tenant key")
        key = (owner_id, name)
        with self._lock:
            expires = self._expires.get(key)
            if expires is not None and now >= expires:
                self.items.pop(key, None)
                del self._expires[key]
            current = self.items.get(key)
            if (current.version if current is not None else None) != expected_version:
                return False
            self.items[key] = workspace
            if ttl is None:
                self._expires.pop(key, None)
            else:
                self._expires[key] = now + ttl
            return True

    def delete(self, owner_id: str, name: str) -> None:
        key = (owner_id, name)
        with self._lock:
            self.items.pop(key, None)
            self._expires.pop(key, None)


class MemoryIdempotencyStore:
    def __init__(self) -> None:
        # (owner, key) -> (fingerprint, record); pending ones are in ``_pending`` too.
        self.items: dict[tuple[str, str], tuple[str, Any]] = {}
        self._pending: dict[tuple[str, str], datetime] = {}
        self._expires: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def _drop(self, key: tuple[str, str]) -> None:
        self.items.pop(key, None)
        self._pending.pop(key, None)
        self._expires.pop(key, None)

    def get(self, owner_id: str, key: str, now: datetime) -> StoredReplay | None:
        k = (owner_id, key)
        with self._lock:
            stored = self.items.get(k)
            if stored is None:
                return None
            pending_until = self._pending.get(k)
            if pending_until is not None:
                if now >= pending_until:
                    self._drop(k)
                    return None
                return StoredReplay(stored[0], stored[1], pending=True)
            expires = self._expires.get(k)
            if expires is not None and now >= expires:
                self._drop(k)
                return None
            return StoredReplay(stored[0], stored[1], pending=False)

    def begin(self, owner_id: str, key: str, fingerprint: str, record: Any, now: datetime) -> None:
        k = (owner_id, key)
        with self._lock:
            self.items[k] = (fingerprint, record)
            self._pending[k] = now + PENDING_REPLAY_TTL
            self._expires.pop(k, None)

    def commit(self, owner_id: str, key: str, now: datetime) -> None:
        k = (owner_id, key)
        with self._lock:
            if k in self.items:
                self._pending.pop(k, None)
                self._expires[k] = now + REPLAY_RETENTION

    def rollback(self, owner_id: str, key: str) -> None:
        with self._lock:
            self._drop((owner_id, key))


class MemoryCostStore:
    def __init__(self) -> None:
        self._totals = SpendTotals()
        self._switch = KillSwitchState()
        self._lock = threading.Lock()

    def add_spend(self, delta: CostEntryDelta, budget: SpendBudget | None, now: datetime) -> SpendTotals:
        with self._lock:
            t = self._totals
            t = replace(
                t,
                total_input_tokens=t.total_input_tokens + delta.input_tokens,
                total_output_tokens=t.total_output_tokens + delta.output_tokens,
                total_usd_micros=t.total_usd_micros + delta.usd_micros)
            if delta.provider_attributed:
                t = replace(
                    t,
                    provider_input_tokens=t.provider_input_tokens + delta.input_tokens,
                    provider_output_tokens=t.provider_output_tokens + delta.output_tokens,
                    provider_usd_micros=t.provider_usd_micros + delta.usd_micros)
            over = over_budget(t, budget)
            self._totals = replace(t, over_budget=False)
            if over and not self._switch.tripped:
                self._switch = KillSwitchState(True, KillSwitchReason.SPEND_CAP, now)
            return replace(t, over_budget=over)

    def totals(self) -> SpendTotals:
        with self._lock:
            return self._totals

    def kill_switch(self) -> KillSwitchState:
        with self._lock:
            return self._switch

    def trip_kill_switch(self, reason: KillSwitchReason, now: datetime) -> bool:
        with self._lock:
            if self._switch.tripped:
                return False
            self._switch = KillSwitchState(True, reason, now)
            return True

    def reset_kill_switch(self, now: datetime) -> None:
        with self._lock:
            self._switch = KillSwitchState()
