"""M1: the per-tenant lock has a timeout (409 TURN_IN_PROGRESS), idle locks are evicted, and the
turn has one wall-clock deadline covering FRAME plus the tool call."""
from __future__ import annotations

import threading

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    frame_turn,
)

from app.domain import Principal, PrincipalKind


def slow_frame(config, entered: threading.Event, proceed: threading.Event):
    inner = frame_turn(config)

    def slow(request):
        entered.set()
        proceed.wait(2)
        return inner(request)

    return slow


def test_a_second_turn_for_one_tenant_times_out_with_a_typed_conflict(monkeypatch) -> None:
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "WORKSPACE_LOCK_TIMEOUT_SECONDS", 0.2, raising=False)
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    gateway = ScriptedGateway(
        [slow_frame(config, entered, proceed), frame_turn(config)],
        config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    first: list = []
    worker = threading.Thread(target=lambda: first.append(env.post(key="slow")))
    worker.start()
    assert entered.wait(2)
    blocked = env.post(key="other")  # a DIFFERENT key for the same tenant
    proceed.set()
    worker.join(5)
    assert blocked.status_code == 409, "the second turn waited for the lock instead of timing out"
    assert blocked.json().get("reason") == "TURN_IN_PROGRESS"
    assert first[0].status_code == 200
    assert env.reserved() == set() and env.turns() == 1  # the timed-out turn held no allowance
    assert len(gateway.requests) == 1


def test_other_tenants_are_not_blocked_by_a_slow_turn() -> None:
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    gateway = ScriptedGateway(
        [slow_frame(config, entered, proceed), frame_turn(config)],
        config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    worker = threading.Thread(target=lambda: env.post(key="slow"))
    worker.start()
    assert entered.wait(2)
    other = env.post(key="b", token="tok-b")
    proceed.set()
    worker.join(5)
    assert other.status_code == 200


def test_idle_locks_are_evicted() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config) for _ in range(30)],
                              config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    version = env.quotas.policies.current().version
    for i in range(30):
        env.orchestrator.run_turn(Principal(f"user-{i}", PrincipalKind.GUEST, version),
                                  "Plan a Toronto outing", f"k{i}")
    assert len(env.orchestrator._workspace_locks) == 0, "locks for finished turns were kept"  # noqa: SLF001


def test_the_lock_is_released_after_a_failed_turn() -> None:
    from app.gateway import TruncationError

    config = make_config()
    gateway = ScriptedGateway([TruncationError(calls=1),
                               frame_turn(config)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    assert env.post(key="a").status_code == 502
    assert len(env.orchestrator._workspace_locks) == 0  # noqa: SLF001
    assert env.post(key="b").status_code == 200


def test_one_deadline_covers_frame_and_the_tool_call() -> None:
    config = make_config()
    clock = [100.0]
    inner = frame_turn(config)

    def frame_that_eats_the_budget(request):
        clock[0] += config.token_ceilings.interactive_budget_seconds + 1  # FRAME took 21 s
        return inner(request)

    gateway = ScriptedGateway([frame_that_eats_the_budget], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config)
    env.orchestrator._pipeline._monotonic = lambda: clock[0]  # noqa: SLF001
    response = env.post()
    assert maps.calls == [], "the tool ran although the run deadline had passed"
    assert response.status_code == 200 and response.json()["warnings"] == ["TOOL_BUDGET_EXHAUSTED"]
    assert response.json()["discovery"] is None and env.turns() == 1
