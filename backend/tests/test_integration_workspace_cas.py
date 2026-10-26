"""Workspace compare-and-set (``put_if_version``): concurrent writers cannot lose updates across
instances, over the memory store and the Firestore adapter on the fake client."""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import (
    ErrorCode,
    ErrorReason,
    FactEdit,
    Principal,
    PrincipalKind,
    TripWorkspace,
    WorkspaceEditRequest,
    evolve,
)
from app.entitlements import default_registry
from app.failures import TurnFailure
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from app.stores import firestore_stores, memory_stores
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional
from app.stores.plans_store import memory_plan_stores
from app.workspaces import WorkspaceConflict, build_workspace_service

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
GUEST = Principal("guest-a", PrincipalKind.GUEST, default_registry().current().version)


def make_state(kind: str):
    clock = lambda: T0  # noqa: E731
    if kind == "memory":
        return memory_stores(clock)
    return firestore_stores(FakeFirestoreClient(clock), clock, transactional=fake_transactional)


@pytest.fixture(params=["memory", "fake-firestore"])
def state(request: pytest.FixtureRequest):
    return make_state(request.param)


def ws(version: int, owner: str = "guest-a") -> TripWorkspace:
    return TripWorkspace(id="w1", owner_id=owner, version=version)


# -- the store contract ---------------------------------------------------------------------------
def test_cas_creates_only_when_absent_and_updates_only_on_the_expected_version(state) -> None:
    store = state.workspaces
    assert store.put_if_version("guest-a", "default", ws(0), T0, None, expected_version=3) is False
    assert store.get("guest-a", "default", T0) is None
    assert store.put_if_version("guest-a", "default", ws(0), T0, None, expected_version=None)
    assert not store.put_if_version("guest-a", "default", ws(1), T0, None, expected_version=None)
    assert store.put_if_version("guest-a", "default", ws(1), T0, None, expected_version=0)
    assert not store.put_if_version("guest-a", "default", ws(2), T0, None, expected_version=0)
    assert store.get("guest-a", "default", T0).version == 1


def test_cas_treats_an_expired_workspace_as_absent(state) -> None:
    store = state.workspaces
    ttl = timedelta(days=7)
    store.put("guest-a", "default", ws(4), T0, ttl)
    later = T0 + timedelta(days=8)
    assert not store.put_if_version("guest-a", "default", ws(5), later, ttl, expected_version=4)
    assert store.put_if_version("guest-a", "default", ws(0), later, ttl, expected_version=None)


def test_cas_refuses_another_tenants_workspace(state) -> None:
    with pytest.raises(ValueError):
        state.workspaces.put_if_version("guest-a", "default", ws(0, "guest-b"), T0, None,
                                        expected_version=None)


def test_racing_increments_never_lose_an_update(state) -> None:
    store = state.workspaces
    writers, rounds = 8, 5
    store.put("guest-a", "default", ws(0), T0, None)
    wins = []
    guard = threading.Lock()

    def worker() -> None:
        done = 0
        while done < rounds:
            current = store.get("guest-a", "default", T0)
            if store.put_if_version("guest-a", "default", evolve(current, version=current.version + 1),
                                    T0, None, expected_version=current.version):
                done += 1
        with guard:
            wins.append(done)

    threads = [threading.Thread(target=worker) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sum(wins) == writers * rounds
    assert store.get("guest-a", "default", T0).version == writers * rounds  # nothing was lost


# -- the user edit across two instances ----------------------------------------------------------
class RendezvousStore:
    """Delegates to a real store; the first ``get`` of each thread waits for the other writer, so
    both read the same version before either writes (two instances with separate locks)."""

    def __init__(self, inner, parties: int = 2) -> None:
        self._inner = inner
        self._barrier = threading.Barrier(parties, timeout=10)
        self._seen = threading.local()

    def get(self, owner_id, name, now):
        found = self._inner.get(owner_id, name, now)
        if not getattr(self._seen, "waited", False):
            self._seen.waited = True
            self._barrier.wait()
        return found

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_two_instances_editing_the_same_version_one_wins_and_one_conflicts(state) -> None:
    shared = RendezvousStore(state.workspaces)
    plans = memory_plan_stores()
    policies = default_registry()
    # Two services, two separate locks: the in-process lock cannot order them.
    services = [build_workspace_service(shared, plans, policies=policies, clock=lambda: T0)
                for _ in range(2)]
    outcomes: list[object] = []
    guard = threading.Lock()

    def edit(index: int) -> None:
        request = WorkspaceEditRequest(
            base_version=0, upsert_facts=(FactEdit(key=f"fact{index}", value="x"),))
        try:
            result: object = services[index].edit(GUEST, request, f"edit-key-{index}-000")
        except WorkspaceConflict as conflict:
            result = conflict
        with guard:
            outcomes.append(result)

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    conflicts = [o for o in outcomes if isinstance(o, WorkspaceConflict)]
    assert len(outcomes) == 2 and len(conflicts) == 1
    assert conflicts[0].current_version == 1
    stored = state.workspaces.get("guest-a", "default", T0)
    assert stored.version == 1 and len(stored.facts) == 1  # exactly one edit landed


# -- the turn path -------------------------------------------------------------------------------
class RacedStore:
    """Another instance writes the workspace between this turn's read and its write."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.raced = False

    def put_if_version(self, owner_id, name, workspace, now, ttl, *, expected_version):
        if not self.raced:
            self.raced = True
            rival = evolve(workspace, version=workspace.version + 7, goal="rival wrote this")
            self._inner.put(owner_id, name, rival, now, ttl)
        return self._inner.put_if_version(owner_id, name, workspace, now, ttl,
                                          expected_version=expected_version)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_turn_that_loses_the_race_is_a_typed_409_stores_nothing_and_is_not_counted(state) -> None:
    raced = RacedStore(state.workspaces)
    quotas = QuotaService()
    orchestrator = TurnOrchestrator(
        quotas, dev_scaffold=True, workspace_store=raced, idempotency_store=state.idempotency,
        clock=lambda: T0)
    with pytest.raises(TurnFailure) as failure:
        orchestrator.run_turn(GUEST, "plan a quiet picnic", "turn-key-0001")
    assert failure.value.status == 409 and failure.value.code == ErrorCode.CONFLICT
    assert failure.value.reason == ErrorReason.TURN_IN_PROGRESS
    stored = state.workspaces.get("guest-a", "default", T0)
    assert stored.goal == "rival wrote this"  # the other writer's update survived untouched
    assert quotas.usage_for("guest-a").successful_turns == 0
    assert state.idempotency.get("guest-a", "turn-key-0001", T0) is None  # nothing pending or stored
    # the same key can be retried and now succeeds on the fresh version
    result = orchestrator.run_turn(GUEST, "plan a quiet picnic", "turn-key-0001")
    assert result is not None and quotas.usage_for("guest-a").successful_turns == 1
