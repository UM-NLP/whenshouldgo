"""M6: one tenant cannot exhaust the thread pool by waiting on its own lock. At most ONE turn runs
and ONE waits per tenant; any further concurrent turn gets the typed 409 TURN_IN_PROGRESS at once
(reservation released, nothing consumed). A global in-flight cap answers 503 with Retry-After.
Neither holds a thread while refusing."""
from __future__ import annotations

import threading
import time

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, frame_turn

from app.frame import FrameIntent


def slow_gateway(config, entered: threading.Event, proceed: threading.Event, extra: int = 3):
    inner = frame_turn(config, intent=FrameIntent.OTHER)

    def slow(request):
        entered.set()
        proceed.wait(5)
        return inner(request)

    return ScriptedGateway([slow] + [frame_turn(config, intent=FrameIntent.OTHER)
                                     for _ in range(extra)], config)


def test_n_parallel_turns_from_one_tenant_occupy_one_running_and_one_waiting_thread() -> None:
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    env = Env(gateway=slow_gateway(config, entered, proceed), config=config,
              discovery=RecordingClient(SyntheticMapsClient()))
    results: list = []

    def attempt(index: int) -> None:
        results.append((index, env.post(key=f"k{index}")))

    workers = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    workers[0].start()
    assert entered.wait(3)  # the first turn holds the tenant lock inside FRAME
    for worker in workers[1:]:
        worker.start()
    deadline = time.monotonic() + 3
    while len(results) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)
    refused = [r for _, r in results]
    assert len(refused) == 4, "extra turns waited for the lock instead of failing at once"
    assert all(r.status_code == 409 and r.json().get("reason") == "TURN_IN_PROGRESS"
               for r in refused)
    users = max(entry[1] for entry in env.orchestrator._workspace_locks.values())  # noqa: SLF001
    assert users <= 2  # one running, one waiting
    # while they are stuck, another tenant and the health check still answer
    assert env.client.get("/healthz").status_code == 200
    assert env.post(key="b", token="tok-b").status_code == 200
    proceed.set()
    for worker in workers:
        worker.join(5)
    codes = sorted(r.status_code for _, r in results)
    assert codes == [200, 200, 409, 409, 409, 409]
    assert env.reserved() == set() and env.turns() == 2  # refused turns consumed nothing


def test_a_refused_turn_leaves_no_reservation_and_can_retry() -> None:
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    env = Env(gateway=slow_gateway(config, entered, proceed), config=config,
              discovery=RecordingClient(SyntheticMapsClient()))
    first = threading.Thread(target=lambda: env.post(key="one"))
    first.start()
    assert entered.wait(3)
    waiter = threading.Thread(target=lambda: env.post(key="two"))
    waiter.start()
    time.sleep(0.2)
    refused = env.post(key="three")
    assert refused.status_code == 409 and "three" not in env.reserved()
    proceed.set()
    first.join(5)
    waiter.join(5)
    assert env.post(key="three").status_code == 200


def test_a_global_in_flight_cap_answers_503_with_retry_after() -> None:
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    env = Env(gateway=slow_gateway(config, entered, proceed), config=config,
              discovery=RecordingClient(SyntheticMapsClient()))
    env.orchestrator._max_inflight = 1  # noqa: SLF001
    first = threading.Thread(target=lambda: env.post(key="one"))
    first.start()
    assert entered.wait(3)
    busy = env.post(key="b", token="tok-b")
    proceed.set()
    first.join(5)
    assert busy.status_code == 503, "the global cap was not enforced"
    assert 1 <= int(busy.headers["Retry-After"]) <= 60
    assert env.reserved("guest-b") == set() and env.turns("guest-b") == 0
    assert env.post(key="b2", token="tok-b").status_code == 200  # capacity is back


def test_the_default_global_cap_is_a_documented_constant() -> None:
    import app.orchestrator as orchestrator

    assert getattr(orchestrator, "MAX_INFLIGHT_TURNS", None) == 32
