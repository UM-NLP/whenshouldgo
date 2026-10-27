"""Builders for the PendingAction tests. Holds no tests. Synthetic only."""
from __future__ import annotations

from fs_backend import client_for, txn_for

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.actions import ActionService
from app.domain import (
    Principal, PrincipalKind, ProposeActionRequest, RoundedPosition, TripWorkspace,
    PendingActionEffect as E, SavedPlaceKind,
)
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry
from app.quota import QuotaService
from app.stores import memory_stores
from app.stores.actions_store import FirestorePendingActionStore, MemoryPendingActionStore

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
VERSION = "actions-test-1"
GUEST = Principal("guest-a", PrincipalKind.GUEST, VERSION)
OTHER = Principal("guest-b", PrincipalKind.GUEST, VERSION)
REGISTERED = Principal("reg-a", PrincipalKind.REGISTERED_FREE, VERSION)
PLACE = "ChIJplaceA_-1"
LABEL = "Quiet corner cafe CANARYLABEL"
KEY = "idem-key-0001"


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def registry(trips: int = 3) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version=VERSION, effective_at=T0, grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(10, trips, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(10, trips, Period.ROLLING_30_DAYS)})
    return PolicyRegistry((policy,), VERSION)


class CountingSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def precheck(self, principal: Principal) -> None:
        return None

    def save_place(self, principal: Principal, *, place_id: str, user_label: str,
                   label_kind: SavedPlaceKind, action_id: str, position: Any) -> None:
        self.calls.append(dict(uid=principal.uid, place_id=place_id, user_label=user_label,
                               label_kind=label_kind, action_id=action_id, position=position))


def make_store(kind: str):
    if kind == "memory":
        return MemoryPendingActionStore()
    client = client_for(kind)
    return FirestorePendingActionStore(client, transactional=txn_for(client))


def workspace(owner: str, ws_id: str = "ws1", plan: int | None = None) -> TripWorkspace:
    return TripWorkspace(id=ws_id, owner_id=owner, active_plan_version=plan)


class Env:
    def __init__(self, kind: str = "memory", *, trips: int = 3, history: bool = False,
                 sink: Any = None, with_workspaces: bool = True, monkeypatch: Any = None) -> None:
        from app import domain as d
        from app.composition import pinned_runtime_config
        self.clock = Clock()
        self.quotas = QuotaService(registry(trips), clock=self.clock)
        self.store = make_store(kind)
        stores = memory_stores(self.clock)
        self.workspaces = stores.workspaces
        config = None
        if history:
            monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({"h-1"}))
            config = d.evolve(pinned_runtime_config(), history_enabled=True,
                              history_notice_version="h-1")
        for p in (GUEST, OTHER, REGISTERED):
            self.workspaces.put(p.uid, "default", workspace(p.uid), self.clock(), None)
        self.sink = sink if sink is not None else CountingSink()
        from app.telemetry import InMemorySink
        self.trace = InMemorySink()
        self.service = ActionService(
            self.store, self.quotas, workspaces=self.workspaces if with_workspaces else None,
            clock=self.clock, runtime_config=config, saved_place_sink=self.sink,
            trace_sink=self.trace)

    def body(self, effect: E = E.LAUNCH_HANDOFF, **kw: Any) -> ProposeActionRequest:
        base: dict[str, Any] = dict(workspace_id="ws1", plan_version=1, effect=effect, target=PLACE)
        if effect == E.SET_REMINDER:
            base["due_at"] = self.clock() + timedelta(hours=2)
            base["user_label"] = LABEL
        if effect == E.SAVE_PLACE:
            base["user_label"] = "Home"
            base["label_kind"] = SavedPlaceKind.HOME
            base["position"] = RoundedPosition(latitude=43.653226, longitude=-79.383184)
        base.update(kw)
        return ProposeActionRequest(**base)

    def propose(self, effect: E = E.LAUNCH_HANDOFF, key: str = KEY, principal: Principal = GUEST,
                **kw: Any):
        return self.service.propose(principal, self.body(effect, **kw), key)
