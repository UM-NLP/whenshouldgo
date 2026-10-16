"""Internal cost ledger (docs/access-tiers.md: provider work already done is recorded even when
the user-facing turn is refunded). In-memory development implementation.

PRODUCTION REQUIREMENT: this ledger is the abuse boundary for paid calls (the daily token
ceiling and the paid-failed-turn limiter read it before every model call), so production needs a
shared, atomic store (Firestore) with the same ``usage_for`` / ``failed_paid_turns`` queries;
an in-process ledger protects one instance only. Entries hold ids, enums, counts and token totals
only, never content.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from .gateway.types import Usage


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


class CostLedger:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._entries: list[CostEntry] = []
        # (principal id, when): a turn that failed AFTER a provider call was sent. Kept apart
        # from the cost entries (those stay one per provider interaction).
        self._failed_turns: list[tuple[str, datetime]] = []
        self._lock = threading.Lock()

    def record(self, entry: CostEntry) -> None:
        stamped = replace(entry, at=self._clock())
        with self._lock:
            self._entries.append(stamped)

    def record_gateway(
        self, principal_id: str, run_id: str, label: str, usage: Usage, model_calls: int,
        cost_usd: Decimal | None = None,
    ) -> None:
        self.record(CostEntry(
            "gateway", principal_id, run_id, label, usage.input_tokens, usage.output_tokens,
            model_calls=model_calls, estimated_cost_usd=cost_usd))

    def record_tool(
        self, principal_id: str, run_id: str, label: str, tool_calls: int, billable_units: int
    ) -> None:
        self.record(CostEntry("tool", principal_id, run_id, label, tool_calls=tool_calls,
                              billable_units=billable_units))

    def record_failed_paid_turn(self, principal_id: str, run_id: str) -> None:
        """Mark a turn that failed after a provider call was sent (``run_id`` is for audit)."""
        _ = run_id
        with self._lock:
            self._failed_turns.append((principal_id, self._clock()))

    def entries(self) -> tuple[CostEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def usage_for(self, principal_id: str, *, window: timedelta | None = None) -> Usage:
        """Token usage of one principal, over a rolling ``window`` ending now (None: all time)."""
        cutoff = self._clock() - window if window is not None else None
        entries = [
            e for e in self.entries()
            if e.principal_id == principal_id and (cutoff is None or (e.at and e.at > cutoff))
        ]
        return Usage(input_tokens=sum(e.input_tokens for e in entries),
                     output_tokens=sum(e.output_tokens for e in entries))

    def failed_paid_turns(self, principal_id: str, *, window: timedelta) -> int:
        cutoff = self._clock() - window
        with self._lock:
            return sum(1 for uid, at in self._failed_turns if uid == principal_id and at > cutoff)
