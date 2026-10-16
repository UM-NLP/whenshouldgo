"""L2: provider usage is not blindly trusted. Recorded input tokens are at least half of the
request's estimate (a deflated body cannot make a call free) and every recorded figure is capped
at the request's worst case (an inflated body cannot blow the ledger). Real gateway stack."""
from __future__ import annotations

from uuid import uuid4

from wiring_support import TRAVEL, real_stack
from test_gateway_support import make_config

from app import telemetry as t
from app.domain import TripWorkspace
from app.frame import build_frame_request
from app.gateway import RunBudget


def expected_request():
    config = make_config()
    return build_frame_request(
        TRAVEL["message"], TripWorkspace(id=str(uuid4()), owner_id="u"), config,
        RunBudget.from_ceilings(config.token_ceilings))


def test_a_deflated_usage_body_is_raised_to_half_the_estimate() -> None:
    request = expected_request()
    env, _, _ = real_stack(prompt_tokens=1, completion_tokens=1)
    assert env.post().status_code == 200
    (entry,) = env.costs.entries()
    assert entry.input_tokens >= request.estimated_input_tokens // 2 > 1
    assert env.events(t.GatewayCall)[0].input_tokens == entry.input_tokens


def test_an_inflated_usage_body_is_capped_at_the_worst_case() -> None:
    request = expected_request()
    env, _, _ = real_stack(prompt_tokens=11_000, completion_tokens=1_100)
    assert env.post().status_code == 200
    (entry,) = env.costs.entries()
    assert entry.input_tokens == request.worst_case_usage.input_tokens
    assert entry.output_tokens <= request.worst_case_usage.output_tokens
    assert env.events(t.GatewayCall)[0].input_tokens == entry.input_tokens


def test_honest_usage_in_range_is_recorded_as_reported() -> None:
    request = expected_request()
    honest = request.estimated_input_tokens
    env, _, _ = real_stack(prompt_tokens=honest, completion_tokens=40)
    env.post()
    (entry,) = env.costs.entries()
    assert (entry.input_tokens, entry.output_tokens) == (honest, 40)


def test_an_error_path_charge_is_capped_too() -> None:
    from maps_simulator import SyntheticMapsClient
    from wiring_support import Env, RecordingClient, ScriptedGateway

    from app.gateway import TruncationError
    from app.gateway.types import Usage

    config = make_config()
    gateway = ScriptedGateway(
        [TruncationError(usage=Usage(input_tokens=900_000, output_tokens=900_000), calls=1)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    env.post()
    (entry,) = env.costs.entries()
    worst = gateway.requests[0].worst_case_usage
    assert (entry.input_tokens, entry.output_tokens) == (worst.input_tokens, worst.output_tokens)
