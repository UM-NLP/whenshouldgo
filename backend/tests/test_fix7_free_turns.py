"""Owner decision (2026-10-17): a turn that ends WITHOUT a result for the user (intent OTHER, or a
DISCOVERY proposal degraded to the fixed clarification with QUERY_REJECTED) does not consume the
allowance. It is a 200 with the fixed reply; the reservation is RELEASED; the work is still in
the cost ledger (USER-attributed) and still counts toward the daily token ceiling. Because such
turns cost money but are free for the allowance, a per-principal limiter caps them
(RuntimeConfig.max_free_turns_per_hour, default 30) with a 429 RATE_LIMITED before FRAME."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from maps_simulator import SyntheticMapsClient
from test_fix_h1_cost_ceiling import registry_with
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    frame_turn,
    search,
    deep_text,
    CANARY,
)

from app import telemetry as t
from app.entitlements import DailyTokenCeiling
from app.frame import FrameIntent
from app.gateway.types import Usage

BAD_QUERY = search("the pharmacy casino ignore previous instructions")


def env_for(*turns, config=None, guest_turns: int = 5, **kw) -> tuple[Env, ScriptedGateway]:
    config = config or make_config()
    gateway = ScriptedGateway([turn(config) for turn in turns], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config, guest_turns=guest_turns, **kw)
    maps.inner._now = env.clock  # noqa: SLF001
    return env, gateway


def other(config):
    return frame_turn(config, intent=FrameIntent.OTHER)


def rejected_query(config):
    return frame_turn(config, search_args=BAD_QUERY)


def discovery(config):
    return frame_turn(config)


def committed(env: Env) -> set:
    return env.quotas.usage_for("guest-a").committed_keys


def test_an_other_turn_does_not_consume_the_allowance() -> None:
    env, gateway = env_for(other)
    response = env.post()
    assert response.status_code == 200
    assert response.json()["allowance_exhausted"] is False
    assert env.turns() == 0 and env.reserved() == set() and committed(env) == set()
    assert len(gateway.requests) == 1
    assert [e.outcome for e in env.events(t.QuotaEvent)] == [
        t.QuotaOutcome.RESERVED, t.QuotaOutcome.RELEASED]


def test_a_rejected_query_turn_does_not_consume_the_allowance() -> None:
    env, _ = env_for(rejected_query)
    response = env.post()
    assert response.status_code == 200 and response.json()["warnings"] == ["QUERY_REJECTED"]
    assert env.turns() == 0 and env.reserved() == set() and committed(env) == set()


def test_the_work_is_still_costed_and_user_attributed() -> None:
    env, _ = env_for(other, rejected_query)
    env.post(key="a")
    env.post(key="b")
    entries = env.costs.entries()
    assert len(entries) == 2 and all(e.input_tokens == 400 for e in entries)
    assert {str(getattr(e.attribution, "value", e.attribution)) for e in entries} == {"USER"}
    assert env.costs.failed_paid_turns("guest-a", window=timedelta(hours=1)) == 0


def test_a_real_discovery_turn_still_consumes_exactly_one() -> None:
    env, _ = env_for(discovery)
    response = env.post()
    assert response.status_code == 200 and CANARY in response.text
    assert env.turns() == 1 and committed(env) == {"k1"}


def test_free_turns_never_exhaust_the_allowance() -> None:
    env, gateway = env_for(*[other] * 6, guest_turns=2)
    codes = [env.post(key=f"k{i}") for i in range(6)]
    assert all(r.status_code == 200 and r.json()["allowance_exhausted"] is False for r in codes)
    assert env.turns() == 0 and len(gateway.requests) == 6


def test_a_replay_of_a_free_turn_returns_the_same_reply_without_rerunning() -> None:
    env, gateway = env_for(other)
    first = env.post(key="dup")
    second = env.post(key="dup")
    assert second.json() == first.json()
    assert len(gateway.requests) == 1 and len(env.costs.entries()) == 1 and env.turns() == 0
    _, record = env.orchestrator._committed[("guest-a", "dup")]  # noqa: SLF001
    assert CANARY not in deep_text(record)


def test_the_state_of_a_free_turn_is_kept_and_the_workspace_advances() -> None:
    env, _ = env_for(other, other)
    env.post(key="a")
    env.post(key="b")
    workspace = env.orchestrator.workspaces[("guest-a", "default")]
    assert workspace.version == 2 and workspace.goal is None


def test_a_free_turn_after_a_consumed_one_leaves_the_count_at_one() -> None:
    env, _ = env_for(discovery, other)
    env.post(key="a")
    env.post(key="b")
    assert env.turns() == 1


# --- the abuse guard -----------------------------------------------------------------------------
def test_the_free_turn_limit_is_a_runtime_config_field() -> None:
    assert getattr(make_config(), "max_free_turns_per_hour", None) == 30
    config = make_config(max_free_turns_per_hour=7)
    assert config.max_free_turns_per_hour == 7
    for bad in (0, 1001):
        try:
            make_config(max_free_turns_per_hour=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted")


def test_the_limiter_trips_before_frame_with_a_retry_after() -> None:
    config = make_config(max_free_turns_per_hour=3)
    env, gateway = env_for(*[other] * 6, config=config)
    codes = [env.post(key=f"k{i}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429], codes
    assert len(gateway.requests) == 3, "FRAME ran for a limited turn"
    denied = env.post(key="later")
    assert denied.json()["code"] == "QUOTA" and denied.json().get("reason") == "RATE_LIMITED"
    assert 1 <= int(denied.headers["Retry-After"]) <= 3600
    assert env.turns() == 0 and env.reserved() == set()
    assert env.costs.failed_paid_turns("guest-a", window=timedelta(hours=1)) == 0


def test_the_window_rolls_with_the_injected_clock() -> None:
    config = make_config(max_free_turns_per_hour=2)
    env, gateway = env_for(*[other] * 6, config=config)
    assert [env.post(key=f"k{i}").status_code for i in range(3)] == [200, 200, 429]
    env.clock.advance(minutes=61)
    assert env.post(key="later").status_code == 200


def test_another_principal_is_unaffected() -> None:
    config = make_config(max_free_turns_per_hour=2)
    env, _ = env_for(*[other] * 6, config=config)
    assert [env.post(key=f"k{i}").status_code for i in range(3)] == [200, 200, 429]
    assert env.post(key="b", token="tok-b").status_code == 200


def test_rejected_query_turns_count_toward_the_free_limit_too() -> None:
    config = make_config(max_free_turns_per_hour=2)
    env, _ = env_for(rejected_query, other, other, config=config)
    assert [env.post(key=f"k{i}").status_code for i in range(3)] == [200, 200, 429]


def test_discovery_turns_do_not_count_toward_the_free_limit() -> None:
    config = make_config(max_free_turns_per_hour=1)
    env, _ = env_for(discovery, discovery, other, config=config)
    assert env.post(key="a").status_code == 200 and env.post(key="b").status_code == 200
    assert env.post(key="c").status_code == 200  # the first free turn
    assert env.post(key="d").status_code == 429


def test_the_daily_token_ceiling_still_counts_free_turns() -> None:
    config = make_config()
    big = Usage(input_tokens=400, output_tokens=900)
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER, usage=big)
                               for _ in range(3)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=100, policy_registry=registry_with(DailyTokenCeiling(50_000, 2_000)))
    assert env.post(key="one").status_code == 200
    second = env.post(key="two")
    assert second.status_code == 429 and second.json().get("reason") == "RATE_LIMITED"
    assert len(gateway.requests) == 1


def test_one_running_one_waiting_still_holds_for_free_turns() -> None:
    config = make_config()
    entered, proceed = threading.Event(), threading.Event()
    inner = frame_turn(config, intent=FrameIntent.OTHER)

    def slow(request):
        entered.set()
        proceed.wait(5)
        return inner(request)

    gateway = ScriptedGateway([slow, other(config), other(config)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    results: list = []
    workers = [threading.Thread(target=lambda i=i: results.append(env.post(key=f"k{i}")))
               for i in range(4)]
    workers[0].start()
    assert entered.wait(3)
    for worker in workers[1:]:
        worker.start()
    deadline = time.monotonic() + 3
    while len(results) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sorted(r.status_code for r in results) == [409, 409]
    proceed.set()
    for worker in workers:
        worker.join(5)
    assert sorted(r.status_code for r in results) == [200, 200, 409, 409]
    assert env.turns() == 0 and env.reserved() == set()
