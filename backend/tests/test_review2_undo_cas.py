"""Review finding 17: a rolled-back turn never overwrites or deletes a later writer's workspace."""
from __future__ import annotations

import pytest
from test_integration_workspace_cas import GUEST, T0, make_state, ws

from app.domain import evolve
from app.failures import TurnFailure  # noqa: F401
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService


class RivalAfterWrite:
    """After this turn's compare-and-set lands, another instance writes a newer workspace."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def put_if_version(self, owner_id, name, workspace, now, ttl, *, expected_version):
        ok = self._inner.put_if_version(owner_id, name, workspace, now, ttl,
                                        expected_version=expected_version)
        if ok:
            self._inner.put(owner_id, name, evolve(workspace, version=workspace.version + 5,
                                                   goal="rival wrote this"), now, ttl)
        return ok

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FailingCommit(QuotaService):
    def commit(self, reservation):  # noqa: ANN001
        raise RuntimeError("commit fault")


@pytest.mark.parametrize("kind", ["memory", "fake-firestore"])
@pytest.mark.parametrize("existing", [False, True])
def test_undo_leaves_a_newer_writers_workspace_alone(kind: str, existing: bool) -> None:
    state = make_state("memory" if kind == "memory" else "fake-firestore")
    if existing:
        state.workspaces.put("guest-a", "default", ws(1), T0, None)
    orchestrator = TurnOrchestrator(
        FailingCommit(), dev_scaffold=True, workspace_store=RivalAfterWrite(state.workspaces),
        idempotency_store=state.idempotency, clock=lambda: T0)
    with pytest.raises(RuntimeError):
        orchestrator.run_turn(GUEST, "plan a quiet picnic", "turn-key-0001")
    stored = state.workspaces.get("guest-a", "default", T0)
    assert stored is not None and stored.goal == "rival wrote this"


def test_delete_if_version_contract() -> None:
    for kind in ("memory", "fake-firestore"):
        state = make_state(kind)
        store = state.workspaces
        store.put("guest-a", "default", ws(3), T0, None)
        assert store.delete_if_version("guest-a", "default", expected_version=2) is False
        assert store.get("guest-a", "default", T0) is not None
        assert store.delete_if_version("guest-a", "default", expected_version=3) is True
        assert store.get("guest-a", "default", T0) is None
        assert store.delete_if_version("guest-a", "default", expected_version=3) is False
