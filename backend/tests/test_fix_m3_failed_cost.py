"""M3: a failed provider call is charged at the gateway's usage when it is trustworthy, otherwise
at the request's worst case (estimated input bound plus max output tokens), and the call is
counted even when the adapter reported zero calls for an attempt that had started."""
from __future__ import annotations

import pytest
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway

from app import telemetry as t
from app.gateway import (
    MalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TruncationError,
)
from app.gateway.types import Usage

ATTEMPTED = [
    ProviderTimeoutError(calls=1),
    ProviderTimeoutError(calls=0),  # an attempt that started but was never counted
    ProviderUnavailableError(calls=1),
    ProviderUnavailableError(usage=Usage(input_tokens=0, output_tokens=0), calls=1),
    MalformedOutputError(calls=2),
    TruncationError(calls=1),
]


@pytest.mark.parametrize("error", ATTEMPTED, ids=lambda e: f"{type(e).__name__}-{e.calls}")
def test_unknown_usage_is_charged_at_the_worst_case(error) -> None:
    config = make_config()
    gateway = ScriptedGateway([error], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    assert env.post().status_code in (502, 503, 504)
    worst = gateway.requests[0].worst_case_usage
    entries = env.costs.entries()
    assert len(entries) == 1, "the failed call was not recorded"
    sent = max(error.calls, 1)  # every call that went out is charged its own worst case
    expected = (worst.input_tokens * sent, worst.output_tokens * sent)
    assert (entries[0].input_tokens, entries[0].output_tokens) == expected
    assert entries[0].model_calls == sent
    call = env.events(t.GatewayCall)[0]
    assert (call.input_tokens, call.output_tokens) == expected
    assert call.model_calls >= 1


def test_a_rejection_before_processing_costs_no_tokens_but_counts_as_a_call() -> None:
    config = make_config()
    error = ProviderRateLimitError(usage=Usage(input_tokens=0, output_tokens=0), calls=1)
    error.rejected_before_processing = True
    gateway = ScriptedGateway([error], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    assert env.post().status_code == 503
    (entry,) = env.costs.entries()
    assert (entry.input_tokens, entry.output_tokens, entry.model_calls) == (0, 0, 1)


def test_trustworthy_usage_from_the_gateway_is_kept() -> None:
    config = make_config()
    gateway = ScriptedGateway(
        [TruncationError(usage=Usage(input_tokens=321, output_tokens=45), calls=1)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    env.post()
    (entry,) = env.costs.entries()
    assert (entry.input_tokens, entry.output_tokens) == (321, 45)
