"""L3: a replay record is visible only after its turn COMMITTED. A concurrent duplicate never gets
the replay of a turn that may still roll back, and a response that cannot be built is a failure
before the commit, not a record replayed as a 500 forever."""
from __future__ import annotations

import threading

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, frame_turn

from app.frame import FrameIntent


def other_env(count: int = 3) -> Env:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER) for _ in range(count)],
                              config)
    return Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)


def test_a_duplicate_during_the_commit_does_not_get_an_uncommitted_replay() -> None:
    env = other_env()
    entered, release = threading.Event(), threading.Event()
    real_commit = env.quotas.commit

    def blocking_commit(reservation):
        entered.set()
        release.wait(5)
        raise RuntimeError("store unavailable")  # the commit FAILS: the turn rolls back

    env.quotas.commit = blocking_commit  # type: ignore[method-assign]
    first: list = []
    worker = threading.Thread(target=lambda: first.append(env.post(key="same")))
    worker.start()
    assert entered.wait(3)
    duplicate = env.post(key="same")  # the state and record are stored but NOT committed yet
    release.set()
    worker.join(5)
    assert duplicate.status_code == 409, "a duplicate was served the replay of an uncommitted turn"
    assert first[0].status_code == 500
    assert env.orchestrator._committed == {}  # noqa: SLF001
    env.quotas.commit = real_commit  # type: ignore[method-assign]
    assert env.post(key="same").status_code == 200 and env.turns() == 1  # the key is reusable


def test_a_duplicate_after_the_commit_is_replayed() -> None:
    env = other_env()
    first = env.post(key="same")
    assert env.post(key="same").json() == first.json()


def test_a_response_that_cannot_be_built_fails_before_the_commit(monkeypatch) -> None:
    import app.pipeline as pipeline

    env = other_env()
    monkeypatch.setattr(pipeline, "REPLY_OTHER", "x" * 10_000)  # violates the response contract
    response = env.post(key="k")
    assert response.status_code == 500
    assert env.orchestrator._committed == {}, "a failure was stored as a replay record"  # noqa: SLF001
    assert env.turns() == 0 and env.reserved() == set()
    assert env.orchestrator.workspaces == {}
    monkeypatch.undo()
    assert env.post(key="k").status_code == 200  # the same key now works
