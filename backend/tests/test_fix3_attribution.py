"""Owner decision (2026-10-16), option A: provider-side failures are RECORDED in the cost ledger
(worst-case rules unchanged, so spend stays visible) but do NOT count toward the user's own
rolling daily token ceiling. Successful turns and abuse-shaped failures still do."""
from __future__ import annotations

from datetime import timedelta

from gateway_fakes import completion_response
from maps_simulator import SyntheticMapsClient
from test_fix_h1_cost_ceiling import registry_with
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RaisingClient,
    RecordingClient,
    ScriptedGateway,
    frame_turn,
    real_stack,
)

from app.costs import CostLedger
from app.discovery import DiscoveryUnavailable
from app.entitlements import DailyTokenCeiling
from app.gateway import TransportResponse, TruncationError
from app.gateway.errors import TransportTimeout
from app.gateway.types import Usage

OUTAGE = TransportResponse(status_code=500, body=None)


def attribution(entry) -> str | None:
    value = getattr(entry, "attribution", None)
    return getattr(value, "value", value)


def test_ten_provider_outage_turns_do_not_rate_limit_a_guest_with_real_ceilings() -> None:
    env, transport, _ = real_stack([OUTAGE] * 30, attempts=3, guest_turns=100, breaker_threshold=1000)
    codes = [env.post(key=f"k{i}").status_code for i in range(10)]
    assert codes == [503] * 10, codes  # the default guest ceiling (6,000 output) is untouched
    assert len(transport.requests) == 30
    entries = env.costs.entries()
    assert sum(e.model_calls for e in entries) == 30
    one_in, one_out = one_attempt()
    assert sum(e.input_tokens for e in entries) == 30 * one_in
    assert sum(e.output_tokens for e in entries) == 30 * one_out  # worst case, all visible


def one_attempt() -> tuple[int, int]:
    env, _, _ = real_stack([OUTAGE], attempts=1)
    env.post()
    (entry,) = env.costs.entries()
    return entry.input_tokens, entry.output_tokens


def test_timeouts_and_provider_rate_limits_are_provider_attributable_too() -> None:
    for failure in (TransportTimeout(), TransportResponse(status_code=429, body=None)):
        env, _, _ = real_stack([failure] * 3, attempts=3)
        env.post()
        assert {attribution(e) for e in env.costs.entries()} == {"PROVIDER"}


def test_entries_carry_a_typed_attribution() -> None:
    env, _, _ = real_stack()  # a successful turn
    assert env.post().status_code == 200
    assert {attribution(e) for e in env.costs.entries()} == {"USER"}
    env2, _, _ = real_stack([completion_response("not json")] * 2)  # abuse-shaped
    assert env2.post().status_code == 502
    assert {attribution(e) for e in env2.costs.entries()} == {"USER"}
    env3, _, _ = real_stack([OUTAGE] * 3, attempts=3)  # provider side
    assert env3.post().status_code == 503
    assert {attribution(e) for e in env3.costs.entries()} == {"PROVIDER"}


def test_a_provider_failure_after_a_good_frame_charges_the_whole_turn_to_the_provider() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config)], config)
    env = Env(gateway=gateway, discovery=RaisingClient(DiscoveryUnavailable(calls=1)),
              config=config)
    assert env.post().status_code == 503
    assert {attribution(e) for e in env.costs.entries()} == {"PROVIDER"}
    assert len(env.costs.entries()) == 2  # the FRAME call and the tool call, both visible


def test_a_deadline_partial_is_provider_attributable() -> None:
    config = make_config()
    clock = [0.0]
    inner = frame_turn(config)

    def slow(request):
        clock[0] += 100
        return inner(request)

    env = Env(gateway=ScriptedGateway([slow], config),
              discovery=RecordingClient(SyntheticMapsClient()), config=config)
    env.orchestrator._pipeline._monotonic = lambda: clock[0]  # noqa: SLF001
    assert env.post().json()["warnings"] == ["TOOL_BUDGET_EXHAUSTED"]
    assert {attribution(e) for e in env.costs.entries()} == {"PROVIDER"}


def test_truncation_stays_user_attributable() -> None:
    # Decision: a cut-off answer cannot be told apart from a model that rambled on valid or
    # hostile input (the output cap is ours), so it counts toward the user's ceiling.
    config = make_config()
    gateway = ScriptedGateway(
        [TruncationError(usage=Usage(input_tokens=5, output_tokens=5), calls=1)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    env.post()
    assert {attribution(e) for e in env.costs.entries()} == {"USER"}


def test_successful_and_abuse_shaped_turns_still_trip_the_ceiling() -> None:
    config = make_config()
    big = Usage(input_tokens=400, output_tokens=900)
    from app.frame import FrameIntent

    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER, usage=big)
                               for _ in range(3)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config,
              guest_turns=100, policy_registry=registry_with(DailyTokenCeiling(50_000, 2_000)))
    assert env.post(key="one").status_code == 200
    assert env.post(key="two").status_code == 429  # 900 used + 1,200 worst case > 2,000
    bad = completion_response("not json", completion_tokens=900)
    env2, _, _ = real_stack([bad, bad] * 4, guest_turns=100,
                            policy_registry=registry_with(DailyTokenCeiling(10**9, 2_500)))
    codes = [env2.post(key=f"k{i}").status_code for i in range(4)]
    assert codes[:2] == [502, 502] and codes[2] == 429, codes  # abuse-shaped turns count


def test_outage_spend_does_not_make_the_ceiling_check_fail_later_for_good_turns() -> None:
    env, _, _ = real_stack([OUTAGE] * 12, attempts=3, guest_turns=100, breaker_threshold=1000)
    for i in range(4):
        assert env.post(key=f"o{i}").status_code == 503
    assert env.post(key="good").status_code == 200  # recovered provider, ceiling untouched


def test_ledger_query_flags_and_running_totals() -> None:
    from datetime import UTC, datetime

    from app.costs import Attribution

    now = [datetime(2026, 10, 16, 12, tzinfo=UTC)]
    ledger = CostLedger(lambda: now[0])
    use = Usage(input_tokens=100, output_tokens=10)
    ledger.record_gateway("u1", "r1", "FRAME", use, 1, attribution=Attribution.USER)
    ledger.record_gateway("u1", "r2", "FRAME", use, 3, attribution=Attribution.PROVIDER)
    ledger.record_gateway("u2", "r3", "FRAME", use, 1, attribution=Attribution.PROVIDER)
    day = timedelta(hours=24)
    assert ledger.usage_for("u1", window=day, user_attributable_only=True) == use
    assert ledger.usage_for("u1", window=day) == Usage(input_tokens=200, output_tokens=20)
    assert ledger.total_usage_for("u1", window=day) == Usage(input_tokens=200, output_tokens=20)
    assert ledger.provider_usage_for("u1") == use
    assert ledger.provider_total == Usage(input_tokens=200, output_tokens=20)
    assert ledger.oldest_usage_at("u2", window=day) is None  # only user spend sets Retry-After
    now[0] += timedelta(days=3)  # pruning forgets entries but never the process-wide total
    ledger.record_gateway("u1", "r4", "FRAME", use, 1, attribution=Attribution.USER)
    assert ledger.provider_total == Usage(input_tokens=200, output_tokens=20)
    assert ledger.provider_usage_for("u1") == Usage(input_tokens=0, output_tokens=0)
