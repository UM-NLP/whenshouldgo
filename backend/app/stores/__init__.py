"""Durable stores behind protocols (ACCESS-02 / #35, decision 26).

``memory_stores`` is the development implementation (per process). ``firestore_stores`` builds the
Firestore adapters over a client; production never passes the in-memory ones. Neither factory
reads the environment or creates cloud resources.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..discovery.cache import DisplayCache
from .killswitch import StoreKillSwitch
from .memory import (
    MemoryConsentLedger,
    MemoryCostStore,
    MemoryIdempotencyStore,
    MemoryQuotaStore,
    MemoryWorkspaceStore,
)
from .protocols import (
    ConsentLedgerStore,
    CostEntryDelta,
    CostStore,
    DisplayCacheStore,
    IdempotencyStore,
    KillSwitchReason,
    KillSwitchState,
    QuotaLimits,
    QuotaStore,
    ReserveOutcome,
    SpendBasis,
    SpendBudget,
    SpendTotals,
    StoredReplay,
    Usage,
    WorkspaceStore,
)


@dataclass(frozen=True)
class StateStores:
    """Every durable store the runtime needs, from one backend."""

    kind: str  # "memory" or "firestore"
    quota: QuotaStore
    consents: ConsentLedgerStore
    workspaces: WorkspaceStore
    idempotency: IdempotencyStore
    display_cache: DisplayCacheStore
    costs: CostStore
    client: Any = field(default=None, repr=False, compare=False)  # Firestore client (None: memory)

    def __repr__(self) -> str:
        return f"StateStores(kind={self.kind!r})"


def memory_stores(
    clock: Callable[[], datetime], *, cache_max_entries: int = 1_000, cache_max_per_owner: int = 50
) -> StateStores:
    return StateStores(
        kind="memory", quota=MemoryQuotaStore(), consents=MemoryConsentLedger(),
        workspaces=MemoryWorkspaceStore(), idempotency=MemoryIdempotencyStore(),
        display_cache=DisplayCache(
            clock, max_entries=cache_max_entries, max_per_owner=cache_max_per_owner),
        costs=MemoryCostStore())


def firestore_stores(
    client: Any, clock: Callable[[], datetime], *, transactional: Callable[..., Any] | None = None,
    max_attempts: int | None = None, cache_max_per_owner: int = 50, replay_codec: Any = None,
) -> StateStores:
    """Adapters over ``client`` (a ``google.cloud.firestore.Client`` or the test fake). With no
    ``transactional`` the real client library's decorator is imported on first use."""
    from .firestore import (
        DEFAULT_MAX_ATTEMPTS,
        FirestoreConsentLedger,
        FirestoreCostStore,
        FirestoreDisplayCache,
        FirestoreIdempotencyStore,
        FirestoreQuotaStore,
        FirestoreWorkspaceStore,
    )

    if replay_codec is None:
        from ..replay_codec import CODEC as replay_codec  # lazy: it imports the orchestrator
    common = {"transactional": transactional,
              "max_attempts": max_attempts or DEFAULT_MAX_ATTEMPTS}
    return StateStores(
        kind="firestore", quota=FirestoreQuotaStore(client, **common),
        consents=FirestoreConsentLedger(client, **common),
        workspaces=FirestoreWorkspaceStore(client, **common),
        idempotency=FirestoreIdempotencyStore(client, replay_codec, **common),
        display_cache=FirestoreDisplayCache(
            client, clock, max_per_owner=cache_max_per_owner, **common),
        costs=FirestoreCostStore(client, **common), client=client)


__all__ = [
    "ConsentLedgerStore", "CostEntryDelta", "CostStore", "DisplayCacheStore", "IdempotencyStore",
    "KillSwitchReason", "KillSwitchState", "QuotaLimits", "QuotaStore", "ReserveOutcome",
    "SpendBasis", "SpendBudget", "SpendTotals", "StateStores", "StoreKillSwitch", "StoredReplay",
    "Usage", "WorkspaceStore", "firestore_stores", "memory_stores",
]
