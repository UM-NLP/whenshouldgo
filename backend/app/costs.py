"""Internal cost ledger (docs/access-tiers.md: provider work already done is recorded even when
the user-facing turn is refunded). In-memory development implementation.

PRODUCTION REQUIREMENT: this ledger is the abuse boundary for paid calls (the daily token
ceiling and the paid-failed-turn limiter read it before every model call), so production needs a
shared, atomic store (Firestore) with the same ``usage_for`` / ``failed_paid_turns`` queries;
an in-process ledger protects one instance only. NOT covered here and tracked as separate work in
that shared store: a global or whole-pilot spend counter, limits on creating new guest identities
and per-IP buckets (a fresh anonymous identity starts with a fresh ledger), and App Check.

Memory is bounded: entries are indexed by principal, a principal's cost entries older than
``retention`` and its failed-turn marks older than ``failure_retention`` are pruned on write, and
every ``sweep_every`` writes all principals are swept so an idle one is dropped too. (Pruning
means the in-memory ledger is not an audit trail; the durable store keeps that.) Entries hold
ids, enums, counts and token totals only, never content.
"""
from __future__ import annotations

import threading
from collections import deque
from enum import StrEnum
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from .gateway.types import Usage
from .stores.memory import MemoryCostStore
from .stores.protocols import CostEntryDelta, CostStore, SpendBudget, SpendTotals, over_budget


def _usd_micros(cost: Decimal | None) -> int:
    """Whole micro-dollars, rounded UP (a cap is never under-counted); an unpriced entry is 0."""
    if cost is None or cost <= 0:
        return 0
    return int((cost * 1_000_000).to_integral_value(rounding="ROUND_CEILING"))


class Attribution(StrEnum):
    """Whose spend an entry is. USER: a successful turn, or a failure the user's own input can
    cause (rejected or malformed output, a rejected patch or arguments, truncation). PROVIDER:
    everything that failed on our provider side (timeouts, resets, 5xx, provider rate limits,
    quota or latch refusals, a kill switch, deadline partials, server faults). Only USER spend
    counts toward the user's daily token ceiling; PROVIDER spend stays visible for cost
    reporting and the whole-pilot budget."""

    USER = "USER"
    PROVIDER = "PROVIDER"


@dataclass(frozen=True)
class CostEntry:
    kind: Literal["gateway", "tool"]
    principal_id: str
    run_id: str
    label: str  # model call role or tool name: an enum value, never free text
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    billable_units: int = 0
    # None when the price is unknown (a failed call is charged at the gateway's worst-case
    # usage, which carries no price).
    estimated_cost_usd: Decimal | None = None
    at: datetime | None = None  # stamped by the ledger's injected clock
    attribution: Attribution = Attribution.USER


class CostLedger:
    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        retention: timedelta = timedelta(hours=24),
        failure_retention: timedelta = timedelta(hours=1),
        sweep_every: int = 100,
        store: CostStore | None = None,
        budget: SpendBudget | None = None,
    ) -> None:
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._retention = retention
        self._failure_retention = failure_retention
        self._sweep_every = max(1, sweep_every)
        self._by_principal: dict[str, deque[CostEntry]] = {}
        # principal id -> when a turn failed AFTER a provider call was sent (abuse-shaped).
        self._failed: dict[str, deque[datetime]] = {}
        # principal id -> when a FREE turn completed (OTHER or a degraded query): they do not
        # consume the allowance, so this is what bounds them. Same retention as the failure marks.
        self._free: dict[str, deque[datetime]] = {}
        self._writes = 0
        # The whole-pilot spend counter and the kill switch live in a ``CostStore``: in memory it
        # covers this process only, the Firestore store is shared by every instance (ACCESS-02,
        # #35). The per-principal windows above stay per process (tracked under #35).
        self._store: CostStore = store if store is not None else MemoryCostStore()
        self._budget = budget
        self._store_failures = 0
        self._lock = threading.Lock()

    # -- writes (prune as they go) --------------------------------------------------------------
    def _prune_locked(self, uid: str, now: datetime) -> None:
        entries = self._by_principal.get(uid)
        if entries is not None:
            cutoff = now - self._retention
            while entries and (entries[0].at is None or entries[0].at <= cutoff):
                entries.popleft()
            if not entries:
                del self._by_principal[uid]
        for table in (self._failed, self._free):
            marks = table.get(uid)
            if marks is not None:
                cutoff = now - self._failure_retention
                while marks and marks[0] <= cutoff:
                    marks.popleft()
                if not marks:
                    del table[uid]

    def _after_write_locked(self, uid: str, now: datetime) -> None:
        self._prune_locked(uid, now)
        self._writes += 1
        if self._writes % self._sweep_every == 0:
            for other in {*self._by_principal, *self._failed, *self._free}:
                self._prune_locked(other, now)

    def record(self, entry: CostEntry) -> None:
        now = self._clock()
        stamped = replace(entry, at=now)
        # The pilot counter first, outside the ledger lock (a store call may block). A store fault
        # must not fail a turn that already spent money: it is counted, and ``spend_blocked``
        # fails closed for new paid work while the store is unreadable.
        try:
            self._store.add_spend(
                CostEntryDelta(entry.input_tokens, entry.output_tokens,
                               _usd_micros(entry.estimated_cost_usd),
                               provider_attributed=entry.attribution == Attribution.PROVIDER),
                self._budget, now)
        except Exception:  # noqa: BLE001
            self._store_failures += 1
        with self._lock:
            self._by_principal.setdefault(entry.principal_id, deque()).append(stamped)
            self._after_write_locked(entry.principal_id, now)

    def record_gateway(
        self, principal_id: str, run_id: str, label: str, usage: Usage, model_calls: int,
        cost_usd: Decimal | None = None, attribution: Attribution = Attribution.USER,
    ) -> None:
        self.record(CostEntry(
            "gateway", principal_id, run_id, label, usage.input_tokens, usage.output_tokens,
            model_calls=model_calls, estimated_cost_usd=cost_usd, attribution=attribution))

    def record_tool(
        self, principal_id: str, run_id: str, label: str, tool_calls: int, billable_units: int,
        attribution: Attribution = Attribution.USER,
    ) -> None:
        self.record(CostEntry("tool", principal_id, run_id, label, tool_calls=tool_calls,
                              billable_units=billable_units, attribution=attribution))

    def record_failed_paid_turn(self, principal_id: str, run_id: str) -> None:
        """Mark a turn that failed after a provider call was sent (``run_id`` is for audit)."""
        _ = run_id
        now = self._clock()
        with self._lock:
            self._failed.setdefault(principal_id, deque()).append(now)
            self._after_write_locked(principal_id, now)

    def record_free_turn(self, principal_id: str) -> None:
        """Mark a completed turn that did not consume the allowance."""
        now = self._clock()
        with self._lock:
            self._free.setdefault(principal_id, deque()).append(now)
            self._after_write_locked(principal_id, now)

    def free_turns(self, principal_id: str, *, window: timedelta) -> int:
        cutoff = self._clock() - window
        with self._lock:
            return sum(1 for at in self._free.get(principal_id, ()) if at > cutoff)

    def oldest_free_turn_at(self, principal_id: str, *, window: timedelta) -> datetime | None:
        cutoff = self._clock() - window
        with self._lock:
            times = [at for at in self._free.get(principal_id, ()) if at > cutoff]
        return min(times) if times else None

    # -- reads ----------------------------------------------------------------------------------
    def entries(self) -> tuple[CostEntry, ...]:
        with self._lock:
            rows = [e for dq in self._by_principal.values() for e in dq]
        return tuple(sorted(rows, key=lambda e: e.at or datetime.min.replace(tzinfo=UTC)))

    def usage_for(
        self, principal_id: str, *, window: timedelta | None = None,
        user_attributable_only: bool = False,
    ) -> Usage:
        """Token usage of one principal over a rolling ``window`` ending now (None: all that is
        retained). With ``user_attributable_only`` only USER-attributed spend counts (what the
        daily ceiling uses); without it the full total, provider-side spend included, is
        returned for cost reporting. Reads only this principal's own entries."""
        cutoff = self._clock() - window if window is not None else None
        with self._lock:
            rows = [
                e for e in self._by_principal.get(principal_id, ())
                if (cutoff is None or (e.at and e.at > cutoff))
                and (not user_attributable_only or e.attribution == Attribution.USER)
            ]
        return Usage(input_tokens=sum(e.input_tokens for e in rows),
                     output_tokens=sum(e.output_tokens for e in rows))

    def total_usage_for(self, principal_id: str, *, window: timedelta | None = None) -> Usage:
        """Everything this principal's turns cost, whoever it is attributed to."""
        return self.usage_for(principal_id, window=window)

    def provider_usage_for(self, principal_id: str, *, window: timedelta | None = None) -> Usage:
        """PROVIDER-attributed spend still retained for this principal."""
        cutoff = self._clock() - window if window is not None else None
        with self._lock:
            rows = [
                e for e in self._by_principal.get(principal_id, ())
                if e.attribution == Attribution.PROVIDER
                and (cutoff is None or (e.at and e.at > cutoff))
            ]
        return Usage(input_tokens=sum(e.input_tokens for e in rows),
                     output_tokens=sum(e.output_tokens for e in rows))

    @property
    def provider_total(self) -> Usage:
        """Whole-pilot running total of PROVIDER-attributed tokens (shared with the store)."""
        totals = self._store.totals()
        return Usage(input_tokens=totals.provider_input_tokens,
                     output_tokens=totals.provider_output_tokens)

    def totals(self) -> SpendTotals:
        """The whole-pilot spend counters, every attribution (see ``SpendTotals``)."""
        return self._store.totals()

    def spend_blocked(self) -> bool:
        """True when new paid work must not start: the pilot budget is reached (the counter that
        tripped the shared kill switch) or the shared store cannot be read (fails closed). With
        no budget configured and a readable store this is always False."""
        if self._budget is None:
            return False
        try:
            return over_budget(self._store.totals(), self._budget)
        except Exception:  # noqa: BLE001
            return True

    def oldest_usage_at(self, principal_id: str, *, window: timedelta) -> datetime | None:
        """When the oldest USER-attributed token-bearing entry (the ones the ceiling counts) was
        written, for Retry-After."""
        cutoff = self._clock() - window
        with self._lock:
            times = [
                e.at for e in self._by_principal.get(principal_id, ())
                if e.at and e.at > cutoff and (e.input_tokens or e.output_tokens)
                and e.attribution == Attribution.USER
            ]
        return min(times) if times else None

    def failed_paid_turns(self, principal_id: str, *, window: timedelta) -> int:
        cutoff = self._clock() - window
        with self._lock:
            return sum(1 for at in self._failed.get(principal_id, ()) if at > cutoff)

    def oldest_failed_paid_turn_at(self, principal_id: str, *, window: timedelta) -> datetime | None:
        cutoff = self._clock() - window
        with self._lock:
            times = [at for at in self._failed.get(principal_id, ()) if at > cutoff]
        return min(times) if times else None

    def now(self) -> datetime:
        return self._clock()
