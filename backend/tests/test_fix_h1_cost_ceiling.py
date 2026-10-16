"""H1: the cost ledger is the abuse boundary. Failed turns refund the user allowance, but the
internal daily token ceiling and a per-principal limiter on PAID FAILED turns still apply,
before the model is called."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RecordingClient,
    ScriptedGateway,
    fact,
    frame_turn,
)

from app.costs import CostLedger
from app.domain import PrincipalKind
from app.entitlements import (
    Allowance,
    DailyTokenCeiling,
    EntitlementPolicy,
    Period,
    PolicyRegistry,
)
from app.frame import FrameIntent
from app.gateway.types import Usage


def registry_with(daily: DailyTokenCeiling) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version="test-ceiling",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(1_000, 10, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(1_000, 10, Period.ROLLING_30_DAYS),
        },
        daily_tokens={PrincipalKind.GUEST: daily, PrincipalKind.REGISTERED_FREE: daily},
    )
    return PolicyRegistry((policy,), policy.version)


def hard_fact_turns(config, count: int):
    return [frame_turn(config, facts=[fact("budget", "free", hard=True)]) for _ in range(count)]


def test_forced_patch_rejections_trip_the_paid_failure_limiter() -> None:
    config = make_config()
    gateway = ScriptedGateway(hard_fact_turns(config, 60), config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000)
    statuses = [env.post(key=f"k{i}").status_code for i in range(50)]
    assert 429 in statuses, "no 429 after 50 forced patch rejections"
    first_429 = statuses.index(429)
    assert statuses[first_429:] == [429] * (50 - first_429)
    assert len(gateway.requests) == first_429 == 10  # default: 10 paid failed turns per hour
    body = env.post(key="after").json()
    assert (body["code"], body.get("reason")) == ("QUOTA", "OVER_BUDGET")
    assert env.turns() == 0 and env.reserved() == set()  # the user allowance is still refunded


def test_the_limiter_threshold_is_a_runtime_config_field() -> None:
    assert make_config().max_paid_failed_turns_per_hour == 10
    config = make_config(max_paid_failed_turns_per_hour=2)
    gateway = ScriptedGateway(hard_fact_turns(config, 5), config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000)
    assert [env.post(key=f"k{i}").status_code for i in range(4)] == [502, 502, 429, 429]
    with pytest.raises(ValueError):
        make_config(max_paid_failed_turns_per_hour=0)


def test_failures_before_any_provider_call_do_not_count_against_the_limiter() -> None:
    config = make_config(max_paid_failed_turns_per_hour=2)
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER) for _ in range(6)],
                              config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000)
    for i in range(6):  # successes are never paid failures
        assert env.post(key=f"k{i}").status_code == 200


def test_the_limiter_window_rolls_over_with_the_injected_clock() -> None:
    config = make_config(max_paid_failed_turns_per_hour=2)
    gateway = ScriptedGateway(hard_fact_turns(config, 6), config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000)
    assert [env.post(key=f"k{i}").status_code for i in range(3)] == [502, 502, 429]
    env.clock.advance(minutes=61)
    assert env.post(key="later").status_code == 502  # the window moved on: a paid call again
    assert len(gateway.requests) == 3


def test_a_different_principal_is_unaffected_by_anothers_limiter() -> None:
    config = make_config(max_paid_failed_turns_per_hour=2)
    gateway = ScriptedGateway(hard_fact_turns(config, 2)
                              + [frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000)
    assert [env.post(key=f"k{i}").status_code for i in range(3)] == [502, 502, 429]
    assert env.post(key="b", token="tok-b").status_code == 200


def test_daily_token_ceiling_blocks_before_the_model_is_called() -> None:
    config = make_config()
    big = Usage(input_tokens=400, output_tokens=900)
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER, usage=big)
                               for _ in range(4)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000,
              policy_registry=registry_with(DailyTokenCeiling(50_000, 2_000)))
    assert env.post(key="one").status_code == 200  # 0 used + 1,200 worst case <= 2,000
    second = env.post(key="two")  # 900 used + 1,200 worst case > 2,000
    assert second.status_code == 429, "daily output ceiling was not enforced"
    assert (second.json()["code"], second.json().get("reason")) == ("QUOTA", "OVER_BUDGET")
    assert len(gateway.requests) == 1 and env.reserved() == set() and env.turns() == 1
    assert env.post(key="b", token="tok-b").status_code == 200  # another principal


def test_daily_ceiling_window_rolls_over_with_the_injected_clock() -> None:
    config = make_config()
    big = Usage(input_tokens=400, output_tokens=900)
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER, usage=big)
                               for _ in range(4)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000,
              policy_registry=registry_with(DailyTokenCeiling(50_000, 2_000)))
    assert env.post(key="one").status_code == 200
    assert env.post(key="two").status_code == 429
    env.clock.advance(hours=24, seconds=1)
    assert env.post(key="three").status_code == 200


def test_a_ceiling_below_one_call_means_no_model_calls() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=1_000,
              policy_registry=registry_with(DailyTokenCeiling(1, 1)))
    assert env.post().status_code == 429 and gateway.requests == []


def test_ledger_usage_for_takes_a_rolling_window_and_an_injected_clock() -> None:
    now = [datetime(2026, 10, 15, 12, tzinfo=UTC)]
    ledger = CostLedger(lambda: now[0])
    ledger.record_gateway("u1", "r1", "FRAME", Usage(input_tokens=10, output_tokens=1), 1)
    now[0] += timedelta(hours=23)
    ledger.record_gateway("u1", "r2", "FRAME", Usage(input_tokens=20, output_tokens=2), 1)
    ledger.record_gateway("u2", "r3", "FRAME", Usage(input_tokens=99, output_tokens=9), 1)
    day = timedelta(hours=24)
    assert ledger.usage_for("u1", window=day) == Usage(input_tokens=30, output_tokens=3)
    now[0] += timedelta(hours=2)
    assert ledger.usage_for("u1", window=day) == Usage(input_tokens=20, output_tokens=2)
    assert ledger.usage_for("u1") == Usage(input_tokens=30, output_tokens=3)  # no window: all
    ledger.record_failed_paid_turn("u1", "r4")
    assert ledger.failed_paid_turns("u1", window=timedelta(hours=1)) == 1
    now[0] += timedelta(hours=2)
    assert ledger.failed_paid_turns("u1", window=timedelta(hours=1)) == 0
    assert len(ledger.entries()) == 3  # failed-turn marks are not cost entries
