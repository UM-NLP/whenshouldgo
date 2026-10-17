"""M2: the paid-failed-turn limiter counts only abuse-shaped failures (malformed or rejected model
output, patch rejected, bad tool arguments, invalid tool result). Provider outages, rate limits,
quota or latch refusals, a kill switch, deadline partials and lock timeouts never count. A
limiter or ceiling denial is its own reason (RATE_LIMITED, 429) with a bounded Retry-After."""
from __future__ import annotations

from gateway_fakes import completion_response
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RaisingClient,
    RecordingClient,
    ScriptedGateway,
    fact,
    frame_turn,
    real_stack,
)

from app.discovery import DiscoveryUnavailable
from app.frame import FrameIntent
from app.gateway import TransportResponse, TruncationError
from app.gateway.types import Usage

LIMIT = 2


def limited_config():
    return make_config(max_paid_failed_turns_per_hour=LIMIT)


def statuses(env: Env, count: int) -> list[int]:
    return [env.post(key=f"k{i}").status_code for i in range(count)]


def roomy():
    """A daily ceiling out of the way, so only the failure limiter is under test."""
    from test_fix_h1_cost_ceiling import registry_with

    from app.entitlements import DailyTokenCeiling

    return registry_with(DailyTokenCeiling(10**9, 10**9))


def test_provider_outages_never_trip_the_limiter() -> None:
    outage = TransportResponse(status_code=500, body=None)
    env, _, _ = real_stack([outage] * 30, attempts=3, config=limited_config(), guest_turns=100,
                           policy_registry=roomy())
    codes = statuses(env, 8)
    assert 429 not in codes, "an outage was counted as abuse"
    assert set(codes) == {503}


def test_provider_rate_limits_never_trip_the_limiter() -> None:
    limit = TransportResponse(status_code=429, body=None)
    env, _, _ = real_stack([limit] * 30, attempts=1, config=limited_config(), guest_turns=100,
                           policy_registry=roomy())
    assert set(statuses(env, 6)) == {503}


def test_a_tripped_or_failing_discovery_never_trips_the_limiter() -> None:
    config = limited_config()
    gateway = ScriptedGateway([frame_turn(config) for _ in range(10)], config)
    env = Env(gateway=gateway, discovery=RaisingClient(DiscoveryUnavailable(calls=1)),
              config=config, guest_turns=100, policy_registry=roomy())
    assert set(statuses(env, 6)) == {503}


def test_a_deadline_partial_is_not_a_failure() -> None:
    config = limited_config()
    clock = [0.0]
    inner = frame_turn(config)

    def slow(request):
        clock[0] += 100
        return inner(request)

    gateway = ScriptedGateway([slow for _ in range(6)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=100, policy_registry=roomy())
    env.orchestrator._pipeline._monotonic = lambda: clock[0]  # noqa: SLF001
    assert set(statuses(env, 5)) == {200}


def test_malformed_model_output_does_count_and_trips_with_rate_limited() -> None:
    bad = completion_response("not json")
    env, _, _ = real_stack([bad, bad] * 6, attempts=3, config=limited_config(), guest_turns=100,
                           policy_registry=roomy())
    codes = statuses(env, 4)
    assert codes[:LIMIT] == [502] * LIMIT and codes[LIMIT:] == [429, 429]
    body = env.post(key="after").json()
    assert (body["code"], body.get("reason")) == ("QUOTA", "RATE_LIMITED"), body


def test_truncated_output_and_patch_rejection_count() -> None:
    config = limited_config()
    gateway = ScriptedGateway(
        [TruncationError(usage=Usage(input_tokens=5, output_tokens=5), calls=1),
         frame_turn(config, facts=[fact("planning_area", "Paris")]),
         frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=100, policy_registry=roomy())
    assert statuses(env, 3) == [502, 502, 429]


def test_the_denial_has_a_bounded_retry_after_header() -> None:
    bad = completion_response("not json")
    env, _, _ = real_stack([bad, bad] * 6, attempts=3, config=limited_config(), guest_turns=100,
                           policy_registry=roomy())
    statuses(env, LIMIT)
    env.clock.advance(minutes=20)
    denied = env.post(key="later")
    assert denied.status_code == 429
    retry = int(denied.headers["Retry-After"])
    assert 1 <= retry <= 3600
    assert abs(retry - 40 * 60) <= 5  # the oldest counted failure ages out in ~40 minutes


def test_the_daily_ceiling_denial_has_retry_after_capped_at_one_hour() -> None:
    from test_fix_h1_cost_ceiling import registry_with

    from app.entitlements import DailyTokenCeiling

    config = make_config()
    big = Usage(input_tokens=400, output_tokens=900)
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER, usage=big)
                               for _ in range(3)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=100, policy_registry=registry_with(DailyTokenCeiling(50_000, 2_000)))
    assert env.post(key="one").status_code == 200
    denied = env.post(key="two")
    assert denied.status_code == 429 and denied.json().get("reason") == "RATE_LIMITED"
    retry = int(denied.headers["Retry-After"])
    assert 1 <= retry <= 3600
    assert "shorter" not in denied.json()["message"].lower()


def test_the_limiter_message_does_not_advise_a_shorter_request() -> None:
    bad = completion_response("not json")
    env, _, _ = real_stack([bad, bad] * 6, attempts=3, config=limited_config(), guest_turns=100,
                           policy_registry=roomy())
    statuses(env, LIMIT)
    message = env.post(key="after").json()["message"].lower()
    assert "shorter" not in message and "try again" in message
