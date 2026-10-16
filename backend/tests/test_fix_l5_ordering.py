"""L5: state is stored BEFORE the quota commit; a failed commit rolls the state back and
releases; a fault after the commit never releases a committed reservation; and a committed
key without a replay record is RESULT_EXPIRED, never a paid re-run."""
from __future__ import annotations

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, frame_turn

from app.frame import FrameIntent


def env_for(turns: int = 3) -> tuple[Env, ScriptedGateway]:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER) for _ in range(turns)],
                              config)
    return Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()),
               config=config), gateway


def test_state_and_replay_record_exist_when_the_commit_runs() -> None:
    env, _ = env_for()
    seen: dict = {}
    real = env.quotas.commit

    def spy(reservation):
        seen["workspace"] = ("guest-a", "default") in env.orchestrator.workspaces
        seen["record"] = ("guest-a", "k1") in env.orchestrator._committed  # noqa: SLF001
        return real(reservation)

    env.quotas.commit = spy  # type: ignore[method-assign]
    assert env.post().status_code == 200
    assert seen == {"workspace": True, "record": True}, "state was stored after the commit"


def test_a_commit_that_fails_before_counting_rolls_state_back_and_releases() -> None:
    env, _ = env_for()

    def failing(reservation):
        raise RuntimeError("store unavailable")

    env.quotas.commit = failing  # type: ignore[method-assign]
    assert env.post().status_code == 500
    assert env.orchestrator.workspaces == {} and env.orchestrator._committed == {}  # noqa: SLF001
    assert env.reserved() == set() and env.turns() == 0


def test_a_fault_after_the_commit_counted_never_releases_the_reservation() -> None:
    env, _ = env_for()
    real = env.quotas.commit

    def commit_then_fault(reservation):
        real(reservation)
        raise RuntimeError("fault after the commit")

    env.quotas.commit = commit_then_fault  # type: ignore[method-assign]
    response = env.post()
    assert env.turns() == 1 and env.reserved() == set()
    assert ("guest-a", "default") in env.orchestrator.workspaces, "state lost after a counted commit"
    assert ("guest-a", "k1") in env.orchestrator._committed  # noqa: SLF001
    assert response.status_code == 200
    env.quotas.commit = real  # type: ignore[method-assign]
    assert env.post().status_code == 200  # the replay works


def test_release_is_not_called_for_a_committed_turn() -> None:
    env, _ = env_for()
    released: list = []
    real_release = env.quotas.release
    env.quotas.release = lambda r: (released.append(r), real_release(r))  # type: ignore[method-assign]
    real = env.quotas.commit

    def commit_then_fault(reservation):
        real(reservation)
        raise RuntimeError("fault after the commit")

    env.quotas.commit = commit_then_fault  # type: ignore[method-assign]
    env.post()
    assert released == []
