"""L1: one run deadline. The tool call gets the REMAINING seconds as its timeout; under a small
floor the tool is skipped (partial result); the lock timeout is never shorter than a turn."""
from __future__ import annotations

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, canary_payload, frame_turn

import app.orchestrator as orchestrator


def run_with_frame_taking(seconds: float):
    config = make_config()
    clock = [100.0]
    inner = frame_turn(config)

    def frame(request):
        clock[0] += seconds
        return inner(request)

    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=ScriptedGateway([frame], config), discovery=maps, config=config)
    maps.inner._now = env.clock  # noqa: SLF001
    env.orchestrator._pipeline._monotonic = lambda: clock[0]  # noqa: SLF001
    return env, maps, config


def test_the_tool_gets_the_remaining_seconds_as_its_timeout() -> None:
    env, maps, config = run_with_frame_taking(12.0)
    assert env.post().status_code == 200
    (timeout,) = maps.timeouts
    assert timeout is not None and 7.9 <= timeout <= 8.1, timeout
    assert timeout <= config.token_ceilings.interactive_budget_seconds - 12 + 0.1


def test_a_fast_frame_leaves_the_full_budget_for_the_tool() -> None:
    env, maps, config = run_with_frame_taking(0.0)
    env.post()
    (timeout,) = maps.timeouts
    assert timeout is not None and timeout <= config.token_ceilings.interactive_budget_seconds


def test_below_the_floor_the_tool_is_skipped_and_the_result_is_partial() -> None:
    env, maps, _ = run_with_frame_taking(19.6)  # 0.4 s left: under the 1 s floor
    response = env.post()
    assert maps.calls == [], "the tool ran with no time left"
    assert response.status_code == 200 and response.json()["warnings"] == ["TOOL_BUDGET_EXHAUSTED"]


def test_a_slow_frame_plus_a_slow_tool_stay_within_the_run_budget() -> None:
    env, maps, config = run_with_frame_taking(10.0)
    env.post()
    (timeout,) = maps.timeouts
    assert timeout is not None and 10.0 + timeout <= config.token_ceilings.interactive_budget_seconds + 0.1


def test_the_lock_timeout_is_not_shorter_than_a_turn() -> None:
    config = make_config()
    assert orchestrator.WORKSPACE_LOCK_TIMEOUT_SECONDS >= config.token_ceilings.interactive_budget_seconds
